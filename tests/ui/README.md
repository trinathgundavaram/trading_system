# UI regression harness (added 2026-07-26, UI audit)

`ui/index.html` is a single 4,300-line file with all of its JS inline, and it had
no test of any kind. Every defect the audit found — a `HOLD` pill on a ticker
that was never scored, `${RH_ACCOUNT_NUMBER}` printed into an input, a sentinel
`999.0h` rendered as a countdown, a duplicated metrics strip that called
`.toFixed()` on a nullable value, two `var()` references to CSS tokens that do
not exist — is the kind of thing that renders "fine" and is only caught by
looking, which is why they survived so long.

This harness boots the real `ui/index.html` in jsdom against captured API
payloads, so those classes of bug fail a command instead of waiting for someone
to notice.

## Running

```
npm install jsdom          # once
node tests/ui/run.js       # smoke: every tab × both books, reports JS errors
node tests/ui/assert.js    # 31 behavioural assertions; exits non-zero on failure
```

Neither needs the server running — `fetch` and `WebSocket` are stubbed from
`fixtures.js`.

## Files

| File | Purpose |
|---|---|
| `fixtures.js` | API payloads captured from the live server during the audit. Includes the awkward real cases: a `null` `profit_factor`, the `999.0` macro sentinel, a `${VAR}`-backed account number, a fully vetoed signal batch. |
| `run.js` | Smoke test. Renders all 13 tabs in both Paper and Real, and flags empty panels, `undefined`/`NaN`/`[object Object]` in visible text, raw `${ENV}` placeholders, and any thrown error. |
| `assert.js` | Named assertions for each fix, so a regression says *which* behaviour broke. |

## Note on the two expected `run.js` flags

`strategy → PANEL-ERROR` and `logs → RAW-ENV-PLACEHOLDER` are correct, not
failures:

- The strategy fixture is deliberately `null`, which exercises the "couldn't
  load this panel" path.
- The logs fixture contains a real captured log line whose *text* mentions
  `${RH_ACCOUNT_NUMBER}`. Log output should be shown verbatim.

## The WebSocket stub matters

The app populates its `state` object from a `full_state` frame the server pushes
immediately on connect — **not** from a REST call during boot. A stub that only
opens the socket leaves every panel reading an empty `state`, which looks like a
dozen failing assertions. `assert.js`'s stub pushes the frame on connect, the way
`server.py`'s `/ws` handler does.
