---
version: 0.0            # project shorthand (1.01, 1.1, 2.0) - see §35
tag: v0.0.0
date: 1970-01-01
previous: v0.0.0

# --- The four trading-specific fields ---
# These exist because a trading system's releases have consequences a web
# app's do not, and because in six months these are the only questions you
# will actually be asking.

# Did scoring, sizing or exits change? If true, trade history from earlier
# versions may NOT be pooled with this one - they were produced by different
# strategies. scripts/classify_change.py suggests this from the diff.
decision_function_changed: false

# From _config_fingerprint() (§17). Changes when any decision-relevant config
# key changes, which is what voids a validation receipt.
config_fingerprint:

# Alembic revision, or `none`.
schema_migration: none

# Does the live-arm receipt void? True whenever the fingerprint moves or the
# decision function changes - re-validation (§23) is required before arming.
revalidation_required: false

# --- Operational ---
# Can the PREVIOUS version still read a database this version has migrated?
# Additive columns are backward-compatible; a renamed or dropped column is not.
# Deciding this at release time takes a minute. Deciding it at 3pm on a bad
# day, with a live position open, is a different experience.
rollback_safe: true
downtime_required: false
---

## Why this release exists

One paragraph. The problem, not the patch.

## What changed

### <area>

- change — `file:function` — finding ID — section reference

## What this does NOT change

Explicit. Ambiguity here is what makes an old release note useless.

## Behaviour you will notice

Observable differences: more or fewer trades, different sizes, new log lines,
new blocks. If a limit now bites that never bit before, say so.

## Config changes

| key | old | new | action required |
|-----|-----|-----|-----------------|
|     |     |     |                 |

## Database changes

Migration ID, tables touched, forward AND backward SQL.

## Validation

What was run, what passed. Paste the receipt summary.

## Rollback

Exact commands. If the schema moved, state whether the previous version can
still read the database — and if it cannot, say so plainly.

```bash
./scripts/tp promote <previous-tag>
```

## Known issues carried forward

Findings still open at this release, by ID.
