// Gate a design variant against the behaviour contract, before anyone looks at how it looks.
//
//   python3 scripts/design-gallery.py --no-serve      # build design/.build first
//   node scripts/check-variant.mjs v2-admission-slips
//   node scripts/check-variant.mjs                    # every built variant
//
// A variant is a fork of app/web/render.mjs. It may change every tag, class and word. It may not
// drop an action, because app.mjs dispatches on those and a missing one is a dead button that no
// screenshot review would catch. v0-today is the reference: whatever actions the shipping design
// emits for a state, a variant must emit for that state too.
//
// This checks contract and safety only. It says nothing about whether the design is any good.
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// AGL_GALLERY_BUILD matches design-gallery.py, so parallel variant builds stay out of each
// other's way.
const BUILD = process.env.AGL_GALLERY_BUILD
  || join(dirname(dirname(fileURLToPath(import.meta.url))), "design", ".build");
const REFERENCE = "v0-today";

const actionsIn = (html) =>
  new Set([...html.matchAll(/data-action="([a-z0-9_-]+)"/gi)].map((m) => m[1]));

function renderAll(mod, states) {
  const out = new Map();
  for (const entry of states) {
    const state = structuredClone(entry.state);
    for (const theme of ["light", "dark"]) {
      state.settings = { ...state.settings, theme };
      const html =
          entry.screen === "settings" ? mod.buildSettings(state)
        : entry.screen === "add-key" ? mod.buildAddKey(state, entry.flow)
        : mod.buildHTML(state);
      out.set(`${entry.id}/${theme}`, html);
    }
  }
  return out;
}

// Provider- and user-controlled strings reach the DOM through innerHTML. Escaping is the only
// thing between a model id and script execution, so every variant re-proves it rather than
// inheriting the claim from the design it forked.
const HOSTILE = '<img src=x onerror="bad()">';
function escapingHolds(mod, states) {
  const base = states.find((s) => s.id === "spending") || states[0];
  const state = structuredClone(base.state);
  if (!state.keys?.length) return null;
  state.keys[0].label = HOSTILE;
  state.keys[0].model = "<script>bad()</script>";
  for (const session of state.pinned_sessions || []) session.key_seat = state.keys[0];
  const html = mod.buildHTML(state);
  if (/<img src=x|<script>bad/.test(html)) return "renders a hostile label unescaped";
  if (!/&lt;(img|script)/.test(html)) return "never escaped the hostile label at all";
  return null;
}

async function check(name, states, reference) {
  const dir = join(BUILD, name);
  const problems = [];
  const mod = await import(join(dir, "render.mjs"));

  for (const fn of ["buildHTML", "buildSettings", "buildAddKey", "buildModelResults"]) {
    if (typeof mod[fn] !== "function") problems.push(`missing export: ${fn}()`);
  }
  if (problems.length) return problems;

  let rendered;
  try {
    rendered = renderAll(mod, states);
  } catch (err) {
    return [`threw while rendering: ${err.message}`];
  }

  for (const [key, html] of rendered) {
    if (!html || html.length < 400) problems.push(`${key}: rendered almost nothing`);
    if (!/theme-(light|dark)/.test(html)) problems.push(`${key}: no theme class on the root`);
    if (/undefined|\[object Object\]|NaN/.test(html)) {
      problems.push(`${key}: leaked undefined/NaN/[object Object] into the markup`);
    }
  }

  if (reference) {
    for (const [key, html] of rendered) {
      const missing = [...actionsIn(reference.get(key) || "")].filter((a) => !actionsIn(html).has(a));
      if (missing.length) problems.push(`${key}: dropped action(s) ${missing.join(", ")}`);
    }
  }

  const leak = escapingHolds(mod, states);
  if (leak) problems.push(`escaping: ${leak}`);

  if (!existsSync(join(dir, "styles.css"))) problems.push("no styles.css");
  return problems;
}

const states = JSON.parse(readFileSync(join(BUILD, "states.json"), "utf8"));
const available = readdirSync(BUILD, { withFileTypes: true })
  .filter((e) => e.isDirectory() && existsSync(join(BUILD, e.name, "render.mjs")))
  .map((e) => e.name);
const wanted = process.argv[2] ? [process.argv[2]] : available;

const referenceMod = available.includes(REFERENCE) ? await import(join(BUILD, REFERENCE, "render.mjs")) : null;
const reference = referenceMod ? renderAll(referenceMod, states) : null;
if (!reference) console.warn(`! ${REFERENCE} missing — action coverage is not being checked`);

let failed = 0;
for (const name of wanted) {
  const problems = name === REFERENCE && reference ? [] : await check(name, states, reference);
  if (problems.length) {
    failed++;
    console.log(`FAIL  ${name}`);
    for (const p of problems) console.log(`        ${p}`);
  } else {
    console.log(`ok    ${name}  (8 states x 2 themes, actions covered, escaping holds)`);
  }
}
process.exit(failed ? 1 : 0);
