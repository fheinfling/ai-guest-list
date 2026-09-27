# Independent CodeQL review of PR #54

Reviewed 2026-09-27. **All five reported alerts are false positives at the reviewed revision, but a separately reproduced plaintext credential leak in the model catalog cache blocks merging.**

## Scope and evidence

The reviewed branch is `byok-key-seats` at `7d74be18e227991d0670e39a68338a3512061818`, matching the locally cached `origin/byok-key-seats`. The diff base is `afde4209d693397c57deaf6d1d60bc9800876ab8`. The working checkout is `ui-refresh` at `e37d85f`; its two additional commits change only design documents, design scripts, and `.gitignore`. Application sources and tests are identical to the reviewed branch. A GitHub API request failed to connect, so this assessment does not attest to a subsequently updated remote PR head. I did not consult another reviewer's assessment.

No application code or repository tests were changed. All additional probes and their outputs are under:

```text
<scratchpad>/codeql-review/
```

Executed evidence:

| Check | Result and limits |
|---|---|
| `python3 -m pytest tests/test_cli_keys.py tests/test_bridge_keys.py tests/test_keyseats.py tests/test_pricing.py tests/test_providers.py tests/test_keyprove.py -q` | **377 passed**. Includes scripted provider errors, Keychain errors, and completed proof/CLI error paths. |
| `python3 -m pytest tests/test_launcher_keyseats.py tests/test_pinned_sessions.py -q` | **69 passed**, despite the new adversarial cache reproduction below. |
| `node --test app/web/render.test.mjs` | **119 passed**. The existing glue tests use a DOM stub, not an HTML parser. |
| `cli_probe.py` | **32 isolated subprocess cases**, both JSON and human output: no full marker on stdout or stderr; **two plaintext cache leaks**. Captured streams are retained as `.stdout` and `.stderr`; summary: `cli-results.json`. |
| `parse5_probe.cjs`, using `web_probe.js` | Actual shipped bundle/event callbacks with parse5 6.0.1 parsing and serialization at HTML sinks. **10 payloads, 453 parse operations, 9,510 assertions passed**; three deliberately unsafe fragments were rejected as positive controls. Summary: `web-results.json`. |
| `launcher_cache_probe.py` | One real launcher price lookup, one declined confirmation, no inference child: **plaintext marker persisted**. Summary: `launcher-cache-results.json`. |
| In-memory `scripts/build-web.py` build | Byte-for-byte equality with `app/web/bundle.js`; no generated file was overwritten. |

The CLI probes use `sk-CODEQL-REVIEW-MARKER-0000`, `Context.for_test()`, controlled HTTP transport responses, and isolated credential storage. They drive `cli.main()` through the real bridge, parsers, state/cache writes, and final printing. Two scenarios, each checked in both output modes, use the real `SecurityKeychain` implementation with a fake executable emitting the marker on **both child output streams**, on success and failure. The parent captures OS stdout/stderr after each CLI process exits.

The HTML probes exercise uppercase/mixed-case script tags, image/event payloads, both quote characters, whitespace/backticks, entity-encoded markup, textarea/style closing tags, and a `javascript:` string. They check parsed elements and attributes, literal values after entity decoding, and re-rendering after events. Chrome failed to start here (exit 134), so this is **HTML5 parser plus event-glue evidence, not a Chromium or WKWebView execution test**. The zero execution-marker count alone is not proof; the useful checks are absence of injected elements/handlers and correct decoded-value round trips. Live Codex/Claude shell-snapshot containment was not rerun or inferred from these checks.

## Verdict table

| Alert | Location | Verdict | Basis |
|---|---|---|---|
| 76 — clear-text sensitive logging | `acctsw/cli.py:137` | **False positive** | The printed result is a bounded response with recursive post-parse secret redaction; raw exceptions are suppressed. Separate cache writes are unsafe, as detailed below. |
| 75 — bad tag filter | `app/web/render.test.mjs:1069` | **False positive** | A negative test assertion, not a production sanitization filter. Character escaping handles uppercase tags too. |
| 74 — bad tag filter | `app/web/render.test.mjs:825` | **False positive** | Same test-only regex issue; proof metadata is escaped and refusal prose comes from fixed mappings. |
| 73 — DOM XSS | `app/web/bundle.js:989` | **False positive; duplicate of 72** | Verified generated equivalent of the source sink. DOM-derived text is escaped again before HTML parsing. |
| 72 — DOM XSS | `app/web/app.mjs:117` | **False positive** | There are real DOM-to-HTML round trips, but the relevant renderers encode their values for their actual contexts. |

## Alert 76: CLI JSON output

### Actual secret flow

1. `_read_key_secret()` (`cli.py:106–113`) reads stdin or uses `getpass`. `GetPassWarning` becomes an exception before the echoing fallback. It does not print the input. `_cmd_keys()` puts it in `message.secret` and calls `bridge.key_action()` (`cli.py:128–132`).
2. `key_action()` validates provider and request fields. `_KeyRequestError` messages use fixed field names and application prose; invalid provider/URL values are not included (`bridge.py:113–128, 154–166`).
3. On add, `keyseats.add()` (`keyseats.py:71–116`) saves the credential in Keychain and returns metadata. Metadata deliberately contains the last four characters as a fingerprint, not the credential. A complete secret in a label, model ID, or custom base URL is rejected before validation/storage. Validation retains status, booleans, timestamp, and a fixed error category; it does not retain provider prose.
4. On model discovery, the wrapped getter first replaces literal secret occurrences in the response body (`bridge.py:168–171`). `fetch_catalog()` parses and **writes the cache before returning**. `key_action()` constructs picker fields from the parsed models (`179–198`).
5. Crucially for the flagged print, `bridge._redact_key()` recursively replaces the full secret in strings, dictionary keys/values, and lists in the completed response (`131–139, 263`). Thus JSON Unicode escapes decoded into a model name are removed from the result **after parsing**, even though they escaped the earlier disk scrub.
6. Unexpected bridge exceptions become generic prose (`259–262`). `_cmd_keys()` also catches exceptions from input, list/Keychain operations, and the bridge and substitutes a fixed message (`133–135`). JSON goes to stdout at 137; human errors go to stderr at 139; human success/model output uses the same redacted result at 140–178. Neither handler logs the original exception or request.

`keys list` bypasses `key_action()` but reads metadata only, without opening Keychain (`keyseats.py:142–145`). `remove` returns a boolean or a generic error. For `prove`, the stored key is read inside `keyprove`; child stdout is consumed as JSON-RPC and stderr is `DEVNULL` (`appserver.py:177–179`). Proof output is restricted to a fixed verdict/error, timestamp, and model; runtime metadata is checked for contamination by `keyhome._secret()`. Exceptions become generic outcomes. `tests/test_keyprove.py:270–292` actually runs both CLI output modes after scripted child replies containing a marker, then checks both streams.

The real Keychain subprocess captures both stdout and stderr (`keychain.py:61–93`), and failed stores become a generic `KeychainError` before reaching the bridge. This matters: catching an exception would not undo a child writing directly to inherited stderr. The additional noisy-child probes verified the capture behavior.

The top-level `main()` prints `AcctswError` text and handles interrupt (`cli.py:425–434`), but key input/backend exceptions on these paths have already been converted to generic results. Context creation occurs before secret reading. No inspected path from `_read_key_secret()` to this sink prints an unsanitized complete credential.

### Evidence and dismissal text

The 32 subprocess cases include successful add/list/remove, model discovery, literal and JSON-escaped provider echoes, HTTP refusal, malformed JSON, `RuntimeError`/`ValueError`/`OSError`/`KeyError` transport exceptions containing the marker, input-read failure, state-save failure, contaminated metadata, and noisy Keychain children. Neither output stream contained the complete marker in either output mode. Some transport failures intentionally produce a saved but unvalidated seat; those successful exits also remained secret-free.

Suggested dismissal:

> At 7d74be1, this prints the return value of the key-operation boundary, not the submitted request. Complete credentials are excluded from seat metadata, provider text is reduced to bounded fields, raw exceptions become fixed messages, and `bridge._redact_key()` recursively scrubs the submitted credential from the completed result after JSON parsing. JSON and human stdout/stderr were checked after execution, including echoed secrets and backend exceptions. This would become a real output leak if a raw request/response/exception were printed, an unredacted early return were added, a new returned value type bypassed recursive redaction, or a child inherited an output stream carrying credentials. This dismissal does not cover the separately demonstrated plaintext catalog cache.

### Does `tests/test_cli_keys.py` prove its claim?

**It tests the right moment, but does not prove an unrestricted “cannot reach stdout/stderr” claim.** It calls `cli.main()` synchronously and inspects output after the bridge, storage, and printing finish. In particular, `test_failures_follow_exit_convention_without_leaking` injects a marker-bearing transport exception and checks both streams after the real CLI returns (`128–141`). This is materially different from checking a prepared home before launching a child.

Coverage has limits:

- Several JSON tests inspect only `.out`, discarding `.err`; human model tests also inspect stdout only. Some list/remove output is merely consumed.
- The fixture substitutes an in-memory Keychain and HTTP getter. `capsys` is not proof against arbitrary subprocess file-descriptor output. The extra subprocess probes address that boundary for the actual Keychain capture implementation.
- The terminal prompt is mocked. The test verifies dispatch and the warning policy, not a real terminal's echo settings.
- The argument-rejection tests prove exit code 2 and absence of a saved seat. They do not prove an incorrectly supplied `--secret KEY` will be absent from argparse's diagnostic. That is outside the stdin/getpass-to-bridge flow.
- These tests neither inspect the catalog cache nor vary JSON encoding of echoed credentials. The related bridge cache test does inspect disk after the fetch, but uses a literal echo only. The new leak is a coverage/input-representation failure, not an early-timing failure.

## Alert 75: regex at render.test.mjs:1069

`assert.doesNotMatch(h, /<img|<script>/)` checks HTML rendered from a pending confirmation whose label/model contain lowercase attack strings. Nothing in the application calls this regex to filter user data. The preceding call actually renders the malicious fixture; the assertion runs afterward, but remains a string assertion.

The relevant controls are `esc(r.key_seat?.label)`, `esc(providerName(...))`, and `esc(r.key_seat?.model)` in `keyConfirmations()` (`render.mjs:746`). They encode `<` regardless of the following tag's case. The independent parser/event probe exercised uppercase and mixed-case payloads through confirmations, including the temporary-element serialization path in `refreshKeyPrompts()`.

Suggested dismissal:

> This regex is a test assertion against a specific lowercase fixture, not a production HTML filter. Confirmation fields are HTML-escaped by `esc()` before interpolation, including uppercase `<SCRIPT>` input. The production control would fail if these fields bypassed escaping or if this regex were repurposed as a sanitizer. The assertion alone is narrower than a general XSS test; parser-based adversarial checks supplement it.

## Alert 74: regex at render.test.mjs:825

`assert.doesNotMatch(h, /<img>|<script>|secret/)` is also test-only. The actual fixture calls `keySeatCard()` with malicious proof metadata. `keyProofStatus()` maps outcome/error to application-authored prose and escapes `proof.checked_at` and `proof.model` (`render.mjs:692–705`). Arbitrary error strings are not interpolated as refusal prose.

The existing test ran successfully. The additional parser probe rendered uppercase/mixed-case tags in model, timestamp, error, seat label, provider, and region fields. No injected tag or handler was produced.

Suggested dismissal:

> This is a negative assertion, not a security filter. Runtime proof rendering uses fixed outcome/error copy and escapes model/timestamp metadata with a character encoder that is independent of tag case. It would become exploitable if raw proof/provider prose or metadata were interpolated into HTML without encoding, or if the test regex were used as a production sanitizer. Expanding the test fixtures would improve coverage but is not the production fix for this alert.

## Alert 73: generated bundle sink

`bundle.js:989` is precisely `app.mjs:117` after the build script strips module syntax and concatenates the files. Calling `build()` in memory produced the exact checked-in bytes. Bundle SHA-256:

```text
378ade4bc6cc5657e5bbff7ae515b3d989bd5767835b285e0ce7947d0557de4d
```

The independent event-glue probe executed this checked-in bundle. This is one source behavior reported twice, not a second independent vulnerable implementation.

Suggested dismissal:

> Generated duplicate of alert 72, verified byte-for-byte against `scripts/build-web.py`. The same renderer encoding controls protect this sink. Reassess if the bundle stops matching its sources or those controls change; generated status alone is not a security defense.

## Alert 72: app.mjs DOM-to-HTML rendering

**Yes, DOM-derived values do reach `innerHTML`.** A dismissal claiming otherwise would be wrong.

| DOM source | Path to HTML sink | Actual control |
|---|---|---|
| Selected model's `dataset.model` (`app.mjs:221–223`) | `keyFlow.model` → review renderer → `root.innerHTML:117` | `esc(flow.model)` at `render.mjs:857`. Encoding `data-model` once is insufficient by itself, since attribute parsing decodes entities; this second encoding is present. |
| Input `.value` for label, secret, base URL (`333–334`) | Flow state → details/review/done → root sink | Escaped in double-quoted attributes and text (`846–848, 857, 861`). |
| Model filter `.value` (`328–330`) | `buildModelResults()` → results `innerHTML:330`; later full picker render | Matching uses strings; no-match copy and the input attribute escape the filter (`790, 801`). |
| Legacy name/token `.value` (`336–337`) | `buildAddSeat()` → root sink | Name attribute/text and textarea content are escaped (`414, 439, 466`). |
| Provider/tool/action datasets | Flow selection → renderers | Static registry/option lookup, fixed branches; displayed arbitrary provider strings elsewhere are escaped. |
| Paid-terminal `dataset.id` (`234`) | Fallback flow label → paid gate | `esc(seat?.label || flow.label)` (`763`). |
| Existing expanded-card datasets (`115`) | Compared with new cards after render | Equality comparisons and class changes only; not interpolated. |

There is **no `textContent` read** in `app.mjs` which is then assigned as HTML. Toasts use a constant HTML skeleton and set `textContent` (`73–76`); other `textContent` assignments are fixed button copy. Clock updates in `render.mjs:108–117` read datasets and write text, not HTML.

The remaining HTML sinks are `insertAdjacentHTML` of `keyConfirmations()` (`123`), the temporary confirmation element (`156`), and copying its serialized `.innerHTML` (`157`). That last operation copies **HTML serialization**, not decoded `.textContent`: escaped text stays escaped on reparse. The parser probe exercised this path during a state push while the add-key flow remained open.

Suggested dismissal:

> DOM-to-HTML flows exist, but the renderer encodes the decoded DOM strings again for their final text, textarea, or double-quoted attribute context. In particular, `dataset.model` reaches `esc(flow.model)` before the flagged root assignment; input values and filter text are also encoded. Toasts use `textContent`, and confirmation copying uses serialization of already escaped renderer output. Uppercase tags, attribute breakouts, entity payloads, and actual model-selection/filter callbacks were exercised with HTML5 parsing. Removing the second encoding, copying decoded `textContent` as HTML, or moving values into JS/CSS/URL or unquoted/single-quoted attribute contexts without the appropriate additional control would make this real.

## Context audit of esc() and raw interpolations

**`esc()` is sufficient for every context where it is currently used; it is not a general context-independent sanitizer.** Its uses are ordinary HTML text, textarea content, and double-quoted attributes. I found no use in an unquoted or single-quoted attribute, a `<style>` body, or an event-handler attribute. Failing to escape `'` is consequently not an attribute breakout in the current templates.

It would not secure a single-quoted attribute against `'`, an unquoted attribute against whitespace, a JavaScript/event-handler context, CSS, or a dangerous URL scheme. Escaping `javascript:...` does not make it a safe `href`. The current `render.mjs` has no dynamic `src` and only one dynamic `href`, at 806: `provider.pricing` comes from the hardcoded HTTPS URLs in `KEY_PROVIDERS` (`640–650`). A custom base URL is shown only as an escaped input value; it is not used as a link/image URL. Native pricing navigation separately selects a hardcoded URL (`menubar.py:780–783`).

The raw HTML interpolations were also traced, rather than assuming every `${...}` passes through `esc()`:

| Raw interpolation group | Source and attacker influence at this revision |
|---|---|
| `seat.status` in classes (`215, 217`) | Potentially unsafe for arbitrary input, but `accounts._assign_statuses()` overwrites every subscription seat status with one of five literals (`accounts.py:251–276`). No provider prose passes through as status. |
| Header counts (`579`), seat/model counts, percentages, countdown/age strings | Counts are backend sums/array lengths; percentages are restricted to numeric input and clamped; date helpers construct numeric/fixed strings. Provider input can influence the numbers, not insert markup. |
| Tool IDs, labels/plans, door/theme classes, region option values | Fixed `codex`/`claude` calls, constant mappings/lists, or comparisons selecting literals. User/provider text used for labels/names/models is separately escaped. |
| CSS widths and accents (`174, 234, 371, 475, 742, 760, 865`) | Width is numeric; accents are constant `TOOL_META` values. Arbitrary tool/provider strings may select a constant or fail lookup, but are not inserted into CSS. No dynamic `<style>` body exists. |
| `provider.pricing`, `flow.provider` in pricing link (`806`); provider IDs/names (`839–840`) | Registry constants. The link branch requires a registry entry with a pricing URL; the DOM provider options originate from that registry. Custom endpoint text cannot supply these values. Remote/custom registry entries would require URL validation and attribute encoding. |
| Settings version/build (`529, 563`) | Local application version and packaged `Info.plist` build number (`acctsw/__init__.py:17–37`), not provider/user metadata. This is a trust assumption, not intrinsic escaping. |
| Proof/status copy; toggle/segment/control labels, icons, hints, checked/disabled fragments | Fixed mappings and local call-site constants. Error strings choose copy; they are not emitted as raw provider prose. |
| Nested `body`, `action`, `expanded`, `fare`, `sessions.join(...)`, mapped cards/results | Deliberately composed HTML from the audited helpers; dynamic strings inside them are escaped or constrained as above. Joining fragments is not itself an escaping control. |

Attacker-influenced model IDs, names, seat labels, provider/region display strings, timestamps, previous-seat labels, and flow errors were included in the audit. No unescaped, reachable HTML interpolation of those strings was established. Raw classes/counts/version and constant-backed URL/CSS interpolation should be reassessed if the backend or registry becomes less constrained. The shipped CSP provides additional restrictions, but none of these dismissals depends on CSP or on the fact that scripts inserted through `innerHTML` may be inert; event-handler and markup injection were considered too.

## Additional finding: full API keys persist in pricing.json

**Confirmed security defect introduced by this diff.** It is separate from alert 76's output sink. Two paths write a complete key into `~/.account-switcher/pricing.json`:

### A. JSON-escaped echo bypasses discovery's scrub

`bridge.py:171` runs `body.replace(secret, "[redacted]")` on the raw JSON response. An endpoint can return the key in a model ID/name with one or more characters represented as JSON Unicode escapes. The raw body then does not contain the literal key. `pricing.parse_catalog()` decodes it (`pricing.py:246, 305`); `fetch_catalog()` serializes that decoded model into the cache (`383–390`). The final `_redact_key()` happens **after** that write.

The reproduction used this valid response construction:

```python
secret = "sk-CODEQL-REVIEW-MARKER-0000"
body = json.dumps({"data": [{"id": "model-" + secret,
                             "name": "name-" + secret}]})
body = body.replace(secret, "".join("\\u%04x" % ord(c) for c in secret))
```

Executing `cli.main(["keys", "models", "openai", "--json"])` with that fake HTTP response and the marker on stdin produced redacted stdout, empty stderr, and a cache containing the complete literal marker in both model fields. Human output gave the same disk result. A literal-echo control produced `[redacted]` in the cache instead. The transport fixture substitutes for the endpoint only; it does not bypass parsing or caching. A user-selected compatible endpoint reaches the same bridge/catalog code with `--allow-unverified`.

### B. Launch-time lookup skips even the literal scrub

`launcher.run()`'s `_offer_key()` calls `pricing.fetch_catalog(provider, secret, get=price_get, ...)` directly (`launcher.py:1115–1125`). Its default transport is `pricing._default_get` (`918`), with no bridge scrub wrapper. A plain response such as this is enough:

```json
{"data":[{"id":"chosen-model","name":"echo sk-CODEQL-REVIEW-MARKER-0000"}]}
```

`launcher_cache_probe.py` creates a normal isolated compatible key seat and invokes the real pinned launcher with a controlled provider response. Its confirmation callback declines, and its child-spawn callback would fail if called. Observed result:

```json
{"exit":75,"catalog_fetches":1,"declined_confirmations":1,"cache_contains_plaintext_secret":true}
```

This demonstrates the write **after the provider request and before/irrespective of paid-session approval**, not merely after preparing an unused home. It requires a cache miss/refresh; a fresh existing cache can delay the request. The model ID can remain completely ordinary: a secret echoed in the display name suffices.

### Trigger, impact, and required fix

A malicious or buggy configured provider echoes the submitted Authorization key in catalog metadata. Discovery with a JSON-escaped echo, or launch-time refresh with even a literal echo, makes the app persist it outside Keychain. An unlucky user can decline the subsequent paid session and still retain that plaintext copy. The endpoint already knows the submitted key; the additional exposure is durable local storage accessible to the user, backups, and tools that read or collect the cache. The reproduced cache mode was `0600` inside the protected data directory, so this is **not** a claim of world-readable storage or an unauthenticated remote key read. Those permissions do not fulfill the intended Keychain-only credential boundary.

Fix at the shared catalog boundary, before persistence: reject or sanitize decoded secret-bearing fields/entries, including nested string keys/values, and ensure every caller gets that protection. Do not rely on raw JSON replacement or response-only redaction. Preserve final output redaction as defense in depth. Handle previously contaminated caches as well; a fresh-cache return must not indefinitely retain an old plaintext copy.

Add regression coverage for literal and partially/fully Unicode-escaped echoes through both CLI/bridge discovery and launcher price lookup. Inspect the entire isolated data directory after the request completes and after a declined confirmation, and inspect stdout/stderr separately. The existing bridge cache test (`test_bridge_keys.py:136–143`) checks disk at the right time but only with literal JSON. Existing launch tests scan disk after completion but use benign catalog names. Both miss the reproduced inputs while passing.

## Recommendation

**Do not merge and fix first.** Correct the catalog cache credential leak and add the adversarial post-request regressions above. Alerts 72–76 themselves can be dismissed for the specific reasons recorded here; their dismissal does not resolve the merge blocker.
