# Deploying v3.0.0

`./service.sh restart` will not pick this up. It bounces the services against
whatever worktree they were installed from — it does not change which version
that is. Deployment here means `tp install` (own worktree + venv + database) then
`tp promote` (repoint the services). See `scripts/tp`'s header.

Run everything from `~/trading_platform` unless stated. If `tp` is on your PATH,
drop the `./scripts/` prefix.

---

## 0. Clear the stale git lock — do this first

`.git/index.lock` is a 0-byte leftover; the sandbox mount denied unlink so it is
still there. `git status` shows phantom deletions until it goes, and
`tp install` runs `git worktree add`.

```bash
cd ~/trading_platform
rm -f .git/index.lock
git status                      # expect: clean, on main
git log --oneline -1            # expect: 0a75750 v3.0.0: the score could not reach its own threshold
git show --stat v3.0.0 | head -20
```

## 1. Push the commit and the tag

```bash
git push origin main
git push origin v3.0.0
```

## 2. Run the release gates that could not run in the sandbox

`scripts/release.sh` was not used — the tag was cut by hand — so these never ran.
They need Postgres and a live environment. Do **not** run `release.sh` itself now;
it would try to cut a *new* version on top of v3.0.0.

```bash
python3 -m pytest tests/ -q          # 3 tests need Postgres up; all 387 should pass
python3 scripts/check_deps.py        # §13 dependency drift
python3 scripts/check_config_secrets.py
python3 scripts/verify_phase1.py --release
python3 scripts/verify_phase2.py
python3 scripts/reconcile.py         # non-blocking, but know before you tag
python3 scripts/tp doctor
```

Stop here if any of the first five fail.

## 3. Take a restore point

```bash
./scripts/tp backup --label pre_v3_0_0
```

## 4. Install v3.0.0

```bash
./scripts/tp install v3.0.0
```

Watch the output for **`pandas_ta <version>`**. If it says
`WARNING: pandas_ta unavailable`, stop and fix the venv before step 5 — every
threshold in `config.yaml` was derived on that backend, and all of my
measurements used the fallback. That warning is the difference between
re-validating and repeating my caveat.

This creates a *fresh database* for v3.0.0. That is the intended design and it
resolves the pooling problem in the release note automatically: v2.4.0's pattern
history stays in v2.4.0's database. **v3.0.0 therefore starts with no EV
history.** `ev_measured=False` is handled as strictly neutral (the cold-start
deadlock was fixed 2026-07-15), so this is safe — but expect no EV signal until
the pattern DB refills.

## 5. Re-validate on the real TA backend — the whole point of the version split

v3.0.0 is not primary yet, so `tp run` forces paper mode. This is the step that
tells you whether my numbers survive contact with `pandas_ta`.

```bash
./scripts/tp run v3.0.0 --backtest -- \
  --tickers BABA CLSK HOOD MARA WFC NU \
  --start 2023-07-27 --end 2026-07-26
```

Compare against the fallback-backend result I recorded:

| | fallback (mine) | pandas_ta (this run) |
|---|---|---|
| trades | 396 | ? |
| win rate | 53.5% | ? |
| profit factor | 1.23 | ? |
| expectancy_R | 0.119 | ? |

Then the mega-cap holdout, which is the one that matters:

```bash
./scripts/tp run v3.0.0 --backtest -- \
  --tickers MSFT GOOGL BAC PFE CMCSA F \
  --start 2023-07-27 --end 2026-07-26
```

I measured PF **1.01**, expectancy **0.004R** there — no edge on liquid
large-caps. If that reproduces on `pandas_ta`, the strategy is not ready for
capital regardless of what the volatile-name numbers say.

## 6. Promote

```bash
./scripts/tp promote v3.0.0
```

This uninstalls v2.4.0's services, writes the primary marker, and reinstalls the
services under label suffix `.v3.0.0` pointed at v3.0.0's database and output
directory.

## 7. Verify

```bash
./scripts/tp list                # v3.0.0 should be primary
./service.sh status              # scheduler / ui / maverick running
./service.sh logs                # watch a cycle complete
curl -s localhost:$(./scripts/tp list | awk '/v3.0.0/{print $3}')/api/health
```

## 8. Do NOT arm live yet

`tp promote` makes v3.0.0 the *only version permitted* to arm — it does not arm
it. The release note says **"Decision function: CHANGED — re-validation required
before arming live"**, and that is not a formality here:

- every measurement in the release note came from `ta_fallback`, not `pandas_ta`
- the mega-cap holdout showed no edge at all
- the measured edge sits in high-volatility names over a single favourable
  2023–2026 window, never walk-forwarded

Let it run in paper until step 5's numbers are reproduced on the real backend and
`learning/walk_forward.py` has been pointed at it across regimes.

---

## Rollback

```bash
./scripts/tp promote v2.4.0
./service.sh status
```

v2.4.0's worktree, venv and database are untouched by any of the above, so this
is immediate and lossless.
