# Intraday momentum (Zarattini/Aziz/Barbon) - scoping, not yet building (2026-07-30)

## What this is

Trinath asked whether the SSRN paper "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)" (Zarattini, Aziz, Barbon; SFI
Research Paper 24-97) is worth building. Prior response in this session
already covered the paper's exact rules and outside critique (a "too good to
be true" flag from an independent reviewer, an unresolved fill-timing
ambiguity, a 43% hit rate implying high payoff convexity, and a follow-up
paper claiming Sharpe >3.0 off further parameter optimization - a red flag
for mining, not confirmation). This doc is the next step Trinath asked for:
data-source options and a rough architecture, BEFORE any code is written.

Nothing here has been built. No files under engine/ or scripts/ have been
touched. Same backtest-only scope boundary as every other addition this
session (§ regime_timed_backtest.py docstring) applies if this goes forward:
new, separate module; no changes to rules/, engine/regime_engine.py, or
any live/paper execution path.

## 1. Why this doesn't reuse the existing pipeline

Everything built so far (`backtest_engine.py`, `regime_timed_backtest.py`,
`equity_curve.py`) runs on daily bars with one decision per symbol per day.
This strategy needs:

- 1-minute OHLCV for SPY (entries/exits keyed to hourly/half-hourly clock
  marks, noise band recomputed every minute)
- 1-minute VIX (or a proxy) for the paper's regime-sensitivity checks
- a per-share commission+slippage cost model ($0.0035/share + $0.001/share
  in the paper), not the flat round-trip-bps model `execution_costs.py`
  currently uses for the swing engine
- an intraday position-sizing loop (vol-target re-sized through the day),
  distinct from `position_sizing.py`'s daily-bar sizing

None of the current data providers wired into `mcp_clients/market_data.py`
(Alpaca, Finnhub, Tiingo, TwelveData, yfinance fallback) were chosen for
minute-bar depth - they're EOD/quote-focused. This needs a new data source.

## 2. Data source options

| Source | Minute history depth | Cost | VIX included? | Notes |
|---|---|---|---|---|
| **Alpaca** (already have keys in `.env`) | ~7+ years (roughly 2016-present) - marketing figure, not yet confirmed against this account/ticker (see open item below) | $0 - already integrated. Correction (2026-07-30): free-tier SIP access is not unconditional - it requires the query end-time be >15 min old. That restriction is irrelevant here since a backtest only ever queries past dates, so historical SIP access older than 15 minutes is available at $0 for this use case. | No (VIX is a CBOE index, not tradable on Alpaca; would need VIXY/VXX ETF minute bars as a proxy) | Cheapest path by far since the keys already exist. Can't reach the paper's full 2007-2024 window - only ~2016 onward. Still spans 2018 vol spike, 2020 crash, 2022 bear, 2023-24 bull - a reasonable multi-regime test even if shorter. |
| **FirstRate Data** | Stocks/ETFs to 2000; VIX index specifically confirmed 1-min bars from 2008-01-02 through 2026-07-02 (per product page) | One-time purchase, ~$300 for the relevant bundle (not a subscription) | Yes, real VIX index minute bars, confirmed coverage above | Only option that reaches close to the paper's actual 2007 start and has real (not proxy) VIX. Data delivered as CSV, needs a one-time ETL step into whatever format the engine reads. |
| **Polygon (now "Massive")** | Minute aggregates; exact historical depth on cheapest tier unclear from public docs | ~$29/mo Starter tier (unclear if minute aggregates or just EOD are included at that tier) | No dedicated VIX product surfaced in this check | Subscription, ongoing cost, needs its own API integration. No clear advantage over Alpaca for this specific use case. |
| **Databento** | Deep tick-level, pay-per-use | Usage-based, likely the most expensive route for a one-off backtest | Not confirmed | Overkill for a single research pass - built for latency-sensitive production, not what's needed here. |

**Recommendation:** start with Alpaca (zero incremental cost, keys already
present, and it removes the question of "is this worth spending money on"
until the shorter-window result says whether the edge shows up at all). If
the Alpaca-window backtest looks promising, THEN spend the ~$300 on
FirstRate Data to extend back to 2007 and get real VIX instead of an ETF
proxy - don't buy the longer history before knowing if the strategy clears
the bar on the cheap data first.

## 3. The one design decision that has to be made before writing code

The independent paper review flagged this as the single biggest open
question and it wasn't resolved by the outside authors either: does a signal
detected at, say, 10:00:00 get filled AT 10:00 (the bar that generated the
signal), or on the NEXT bar (10:01+, once the breakout is confirmed and a
real system would actually have been able to react)?

At swing-trade daily-bar frequency this distinction barely matters. At
hourly/half-hourly intraday decision points on a mean-reverting-adjacent
noise-band breakout, it can be the difference between a real edge and an
artifact of assuming an impossibly fast fill. This needs to be built as an
explicit, named parameter from day one (`fill_timing: "same_bar" |
"next_bar"`) and the sweep needs to report both, the same way the regime
work reported sensitivity to `sma_period`/`buffer_pct` - not picked once and
hidden.

## 4. Rough module shape (if/when this gets built)

Mirroring the existing convention (new file, reuses production pieces where
genuinely shared, everything else self-contained and default-off):

- `engine/intraday_momentum_backtest.py` - noise-band computation, entry/exit
  clock-mark logic, vol-target sizing, EOD flatten. New module, does not
  touch `backtest_engine.py`.
- `engine/intraday_execution_costs.py` (or a per-share mode added to the
  existing `execution_costs.py`) - $/share commission + slippage instead of
  bps-of-notional, since that's what the paper's cost assumption is and bps
  scales wrong at this trade frequency.
- `scripts/intraday_momentum_sweep.py` - sweeps noise-band multiplier,
  fill-timing, and the two trailing-stop variants (band-only vs
  band-or-VWAP), same live-progress-printing / resumable-checkpoint pattern
  already added to the daily sweep scripts this session.
- `tests/test_intraday_momentum_backtest.py` - unit tests on synthetic
  minute bars for: noise-band math, hysteresis-free breakout detection,
  both fill-timing modes, EOD forced flatten.
- `config.yaml` - new `intraday_backtest:` block, default-off, same
  posture as `use_dynamic_sizing`/`execution_cost_bps`.

## 5. Phase plan and phase-1 success criteria (added 2026-07-30)

Two phases, gated - don't spend on FirstRate Data until phase 1 earns it:

- **Phase 1 (Alpaca, $0):** build the module, run it on the ~2016-2026
  window, under **next-bar** fills (the conservative assumption), the
  per-share cost model, and a **frozen out-of-sample / walk-forward split**
  (same discipline as the regime-timing work - select params on an early
  slice, freeze, test unseen on the rest). Success = the edge survives all
  three of those at once. If it doesn't, stop there - that result is the
  answer to "is this worth building," and it was reached for $0.
- **Phase 2 (FirstRate Data, ~$300), only if phase 1 succeeds:** extend to
  ~2007-2024 with real VIX minute bars to see whether phase 1's result
  holds over the paper's original window and to run the paper's VIX-regime
  sensitivity checks for real instead of on a proxy.

The published follow-up paper claiming Sharpe >3.0 via "improvements to
noise-boundary-based intraday momentum strategies" is not being treated as
independent confirmation - further optimization on the same framework by
different authors is exactly the kind of result that raises data-mining
concern rather than resolving it. If anything it argues for holding phase 1
to a stricter out-of-sample bar, not a looser one.

## 6. Open questions to resolve before coding starts

1. Confirm Alpaca's actual minute-bar start date for SPY specifically (the
   "~7+ years" figure above is a general claim from Alpaca's docs, not
   verified against this account/ticker yet).
2. Decide the VIX proxy for the Alpaca-only window (VIXY minute bars vs.
   skipping the VIX-regime part of the paper's analysis entirely for the
   first pass) - phase 2's real VIX data resolves this properly, but phase 1
   needs an explicit choice either way.
3. Confirm same-bar vs next-bar fill will be built as a dual-report sweep
   axis, not a single silent choice - phase 1's success criterion above
   already commits to next-bar as the qualifying run, with same-bar reported
   alongside for comparison, not substituted in if next-bar fails.
4. Confirm the walk-forward/train-freeze-test split design for phase 1
   before the first sweep runs, not after seeing results - deciding the
   split post-hoc would reintroduce the same look-ahead risk this criterion
   exists to prevent.

Status: scoping approved, not building yet. No further action pending
Trinath's answers to the four items above.

## 7. Phase 1 build status (2026-07-31)

Built and unit-tested, network-independent, zero live-path changes:

- `engine/intraday_momentum_backtest.py` - noise bands, VWAP, clock-mark
  entry/exit state machine, both trailing-stop variants, EOD flatten,
  `fill_timing` as a first-class dual-report parameter, vol-target
  leverage metadata (not yet compounded into a portfolio equity curve -
  see the module's own docstring).
- `engine/intraday_execution_costs.py` - $/share cost model, defaults to
  the paper's own disclosed commission+slippage (not zero - see its
  docstring for why that default is inverted from this codebase's usual
  posture).
- `tests/test_intraday_momentum_backtest.py`,
  `tests/test_intraday_execution_costs.py` - 35 tests, all passing,
  hand-worked arithmetic on synthetic bars (noise-band math, gap
  adjustment, both stop modes, both fill-timing modes, no-lookahead
  checks on the sigma/vol calculations, EOD flatten, cost model). Full
  existing repo test suite re-run alongside - no regressions introduced.
- `scripts/intraday_momentum_sweep.py` - 8-combo grid
  (band_multiplier x stop_mode), checkpointed/resumable, reads a local
  bars file (see below) - explicitly labeled in its own output as
  in-sample/exploratory, not a validated result.
- `scripts/walk_forward_intraday_momentum.py` - the actual phase-1 gate:
  TRAIN-only selection (net profit factor, min-trade floor), frozen
  TEST run under next_bar fills, same_bar reported alongside for
  comparison only. Split policy (train_frac=0.7 default, or an explicit
  --split-date) locked in the script's own docstring before any real run.
  Both scripts smoke-tested end-to-end (including checkpoint resume) on
  a synthetic dataset - mechanically verified, not yet run for real.
- `config.yaml` - new `intraday_backtest:` block, backtest-only.

**BLOCKED: no real multi-year 1-minute SPY data reachable from this
environment.** Verified directly, not assumed: this sandbox has zero
network route to data.alpaca.markets/api.alpaca.markets (curl confirms
only package registries - pypi/npm/github - are allowlisted; even a plain
request to google.com fails the same way). The one working data connector
available in this session (a Robinhood-linked market-data MCP tool) DOES
return real minute bars, but only for roughly the trailing month - every
date tested from mid-June 2026 back through 2016 returned a flat,
zero-volume, `interpolated: true` placeholder priced at whatever today's
price happens to be, not a real historical quote. Using that as phase-1's
dataset would silently produce a meaningless backtest, not a small one -
exactly the kind of trap this session has been built around catching, so
it was not used.

This is why open item #1 from sec 6 is still open. Phase 1's code is
ready to run the moment a real dataset exists at `--bars-csv <path>`;
getting that file is the one remaining step, and it requires action
outside this sandboxed environment.

`scripts/fetch_alpaca_minute_bars.py` was built for exactly that step -
run it on a machine with real network access to Alpaca (this repo's
existing ALPACA_API_KEY/ALPACA_API_SECRET, no new keys needed). It
hard-codes feed=sip (not the iex feed mcp_clients/market_data.py uses for
live quotes) since a historical query >15min old gets full SIP for free -
see the script's own docstring for why iex would silently corrupt this
strategy's VWAP the same way it once corrupted volume_ratio in production.
Resumable (appends past a killed run's last saved timestamp). Verified as
far as this sandbox allows: request construction, resume logic, and error
handling all check out; the live Alpaca call itself was confirmed BLOCKED
from here (a clean, expected connection failure, not a code bug) - it has
not yet been run successfully end-to-end against the real API.

## 8. Phase 1 REAL result (2026-07-31)

Trinath ran `scripts/fetch_alpaca_minute_bars.py` on a machine with real
Alpaca access. Result: `spy_1min.csv` (2,118,199 raw rows, SIP feed) and
`vixy_1min.csv` (1,198,299 rows), both 2016-01-01 through 2026-07-30.
Validated before use: 2,658 real RTH sessions after `prepare_bars`
filtering, zero null OHLC, zero zero-volume RTH rows, only 15 short
sessions (expected early closes, not data gaps) - clean data, not another
placeholder trap.

Ran `scripts/walk_forward_intraday_momentum.py` exactly as designed in
sec 5/6 above, no parameters overridden from the locked defaults
(train_frac=0.7, min_train_trades=30, next_bar fills for
selection+freeze). Split landed at TRAIN = 2016-01-04..2023-05-24 (1,860
sessions), TEST = 2023-05-24..2026-07-30 (798 sessions).

TRAIN-selected winner: `band_multiplier=1.5, stop_mode=band_or_vwap`
(TRAIN PF-net 1.251, n=1,166, win rate 44.3% - notably close to the
paper's own reported ~43% hit rate). Frozen TEST run: **PF-net = 1.01**,
n=518, win rate 40.3%, avg 0.0011%/trade net.

**Mechanically, this clears the pre-committed bar (PF-net > 1.0). Read
honestly, it does not look like a result worth trusting:**
  - avg 0.0011%/trade net is indistinguishable from zero - over 518 TEST
    trades that's roughly +0.6% cumulative, unlevered, before any
    real-world friction beyond the modeled $/share cost (market impact at
    size, latency, partial fills).
  - TRAIN PF 1.251 -> TEST PF 1.01 is a large degradation, the same shape
    of warning sign sec 9c/9d of the daily regime work already taught
    this session to distrust.
  - Fragility check (informational, NOT used for selection - computed
    after the freeze, for context only): running all 8 combos on the TEST
    window shows real parameter sensitivity. `band_mult=1.2, stop=band`
    would have scored TEST PF 1.081 - BETTER than the combo TRAIN actually
    picked - while `band_mult=1.0` and `1.2` under `band_or_vwap` scored
    TEST PF *below* 1.0 (0.966, 0.921 - net losers). A real, structural
    edge should not flip sign between band_multiplier=1.0 and 1.5 on the
    same stop mode over the same window.
  - fill_timing turned out NOT to be the swing factor here - same_bar vs
    next_bar gave nearly identical TEST PF (1.012 vs 1.01) for the winning
    combo. Worth recording since it was flagged as the single biggest risk
    going in; it wasn't the one that mattered this time.

**Read together: phase 1 does not clear its own bar in spirit, only in the
letter of a bare PF>1.0 threshold.** This looks like noise sitting at the
breakeven line with real parameter fragility around it, not a signal.
Per the scoping doc's own standing rule ("if it doesn't, stop there"),
this is a stop point, not a green light for phase 2 (FirstRate Data,
~$300) - spending on the longer real-VIX dataset to chase a result this
thin and this fragile would repeat the exact mistake this session's whole
methodology exists to avoid.

`vixy_1min.csv` was fetched but not yet used - the VIX-regime sensitivity
analysis the paper also runs was never reached, since the core edge did
not clear its own gate first.
