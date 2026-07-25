# Phase 0 — Foundation

*Version control and version management · ships v1.0.0*

Nothing in this phase changes a trading decision. That is deliberate: it is the
instrument every later phase is measured with. Every phase after this one
changes trading behaviour, and until you can tag a version, write down what
changed, and re-run last week's build to compare, you cannot tell an
improvement from a regression.

## What was done

| Step | What | § | Where it lives |
|------|------|---|----------------|
| 0.1 | Disarm live execution | §2 | `config.yaml` — `auto_trade: false`, `watch_execute: WATCH`, `live_execution_enabled: false` |
| 0.2 | Credentials out of the tree | §3, §34.3 | `storage/secrets.py`, `config_loader.py`, `.env.template`; `config.yaml` now uses `${RH_ACCOUNT_NUMBER}` / `${UI_AUTH_TOKEN}` |
| 0.3 | Ignore rules, secret scanning, safe first commit | §34 | `.gitignore`, `.pre-commit-config.yaml`, `scripts/check_config_secrets.py` |
| 0.4 | Version scheme | §35 | `scripts/version.py`, `scripts/classify_change.py` |
| 0.5 | Release notes, enforced | §36 | `CHANGELOG.md`, `docs/releases/TEMPLATE.md`, `scripts/hooks/pre-push` |
| 0.6 | One-command release | §37 | `scripts/release.sh`, `storage/version.py`, `migrations/001_app_version_stamps.sql` |
| 0.7 | Pin every dependency; surface the TA backend | §13 | `requirements.txt`, `scripts/check_deps.py`, `scripts/pin_requirements.py`, `engine/ticker_analyzer.py` |
| 0.8 | `TP_OUTPUT_DIR` indirection | §38.2 | `storage/paths.py`; wired into `scheduler.py`, `storage/log_setup.py`, `main.py` |
| 0.9 | Run any version, side by side | §38 | `scripts/tp`, `service.sh` (`LABEL_SUFFIX`), `engine/live_trader.py` (`TP_FORCE_PAPER`) |
| 0.10 | Secrets, backups, standing audit | §39 | `scripts/tp secrets` / `backup` / `doctor` |

## The two ideas worth remembering

**The version number answers a question conventional semver does not ask.**
Semver asks "does this break a caller's code?" — meaningless here, since this
system has no external callers. The question that matters is: *does this change
alter the decision function — the mapping from market data to a buy, a size, or
an exit?* If yes, every trade recorded before the release was produced by a
different strategy, and pooling the two sets of results is a measurement error.
Deleting `* b.qual_mult` from the scoring sum (§19) is one line and a 2.0.
`scripts/classify_change.py` applies this rule to the diff so it does not depend
on anyone remembering it.

**Reproducibility must not extend to reproducing the unsafe configuration.**
`scripts/tp` makes every past tag runnable — and v1.0.0's own committed
`config.yaml` has all three live-execution gates open. Without a veto, `tp run
v1.0.0` to reproduce a backtest would faithfully arm live trading against the
real account. `TP_FORCE_PAPER=1` (§38.4) is set for every version that is not
the one named in `~/tp/PRIMARY`, and it is checked *above* config, so no
checked-out config can override it.

## Exit criteria

```bash
git describe --tags                     # v1.0.0
git status --porcelain                  # empty
./scripts/tp install v1.0.0
./scripts/tp run v1.0.0 --backtest      # reproduces the 0-trade result
./scripts/tp doctor                     # ALL CLEAR
python3 -c "from engine import live_trader as lt, config_loader; \
            print(lt.is_live_mode(config_loader.load_config_dict()))"   # False
```

## What is NOT done, and is not supposed to be

- **Credential rotation (§3).** The tooling is here; the rotation is not.
  Changing the Robinhood password, re-enrolling 2FA (which invalidates the old
  TOTP seed), and revoking every API key has to be done from the provider
  dashboards. The NVIDIA key that was pasted into a chat window must be
  **revoked**, not reused.
- **The test suite (§12, E-2).** 33 of 58 tests cannot run. `release.sh` warns
  and asks rather than blocking, because a gate nobody can pass is a gate that
  gets removed. Phase 2 step 2.1 fixes this and the warning becomes a hard fail.
- **Alembic (§28).** `migrations/` holds plain SQL with forward and backward
  blocks until Phase 5.
- **Anything that changes a trading decision.** That starts at Phase 1.

## Next

Phase 1 (§2, §3, §4, §5, §6, §17) — contain the immediate risk. One day, four
changes, none of which touches the decision function. Ships as v1.0.1.
