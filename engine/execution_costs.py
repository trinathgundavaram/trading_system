"""Execution cost (slippage + spread) model for the Stage 1 backtest replay
(2026-07-28, directive #2 from Trinath's post-backtest review: "Add execution
costs now, even a simple slippage/spread model, because the current edge is
not large enough to ignore friction").

CONTEXT. The 2026-07-28 replay (2542 trades, PF 1.14, expectancy_r positive
but thin) computed outcome_pct/outcome_r from RAW bar-triggered prices - the
stop/target/close level a bar touched, with no haircut for the fact that a
real fill never happens exactly at the quoted level. A post-hoc sensitivity
check against that run's results.json (round-tripping 0/5/10/20/30/50 bps off
every trade's outcome_pct) showed PF falling from 1.14 at 0bps to 1.05 at
20bps, 1.01 at 30bps, and 0.93 at 50bps - i.e. the unfiltered edge does not
survive a cost assumption that would be unremarkable for a low-liquidity
swing name. That sensitivity table is what this module makes real and
built-in, instead of a one-off spreadsheet exercise.

SCOPE - this is a Stage 1 backtest-only model. It touches NOTHING in the live/
paper execution path (engine/order_execution.py, engine/live_trader.py,
engine/paper_trader.py never import this module). It exists purely so a
replay's reported PF/expectancy reflects realistic friction instead of
frictionless fills, the same way rules/execution_quality.py's spread/slippage
SCORING already exists for live signal quality - this module is the backtest
analogue for P&L, not a duplicate of that scoring logic.

DEFAULT IS OFF (0 bps) so this cannot silently change any existing run's
numbers - config.yaml's backtest.execution_cost_bps must be set explicitly
(or --execution-cost-bps passed to run_backtest.py) to activate it. That
mirrors this project's convention elsewhere (e.g. use_dynamic_sizing in
v3.4.0) of shipping new knobs default-off and letting a deliberate config
change turn them on.

MODEL. A single round-trip cost in basis points (entry + exit slippage/spread
combined), split evenly across the two fills: the effective entry price is
haircut UP by half the round-trip bps (you pay the spread/slippage buying),
the effective exit price is haircut DOWN by half (you pay it again selling).
This is deliberately simple - a flat bps haircut, not a liquidity-aware model
keyed off dollar volume or the existing execution_quality slippage_bands -
because the goal right now is "does the edge survive ANY realistic friction
assumption," not a precise per-ticker cost curve. A liquidity-aware version
(reusing execution_quality's dollar_volume_bands) is a reasonable future
step if the flat model shows the edge is cost-sensitive at the boundary
rather than clearly dead or clearly robust.

NOTE ON RISK BASIS. outcome_r's risk_per_share/risk_pct denominator (see
_exit_result in backtest_engine.py) is deliberately left on the RAW entry
price, not the cost-adjusted one - the stop machine's risk framing is about
where the stop sits relative to the actual fill the position was opened at,
which the stop machine itself has no visibility into cost assumptions about.
Only the P&L numerator (outcome_pct, and therefore outcome_r) is cost-
adjusted.
"""

DEFAULT_ROUND_TRIP_BPS = 0.0


def get_round_trip_bps(cfg: dict) -> float:
    """Reads backtest.execution_cost_bps from cfg (0 = off, the default).
    Tolerant of a missing/malformed key - a bad config value should degrade
    to "no cost model" rather than crash a replay that was otherwise fine."""
    cfg = cfg or {}
    bt_cfg = cfg.get("backtest", {}) or {}
    raw = bt_cfg.get("execution_cost_bps", DEFAULT_ROUND_TRIP_BPS)
    try:
        bps = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ROUND_TRIP_BPS
    return bps if bps > 0 else DEFAULT_ROUND_TRIP_BPS


def apply_execution_costs(entry_price: float, exit_price: float, cfg: dict) -> tuple:
    """Returns (effective_entry_price, effective_exit_price) after haircutting
    for slippage/spread, per the module docstring's split-the-round-trip
    model. A no-cost cfg (the default) returns the inputs unchanged."""
    bps = get_round_trip_bps(cfg)
    if bps <= 0 or not entry_price or not exit_price:
        return entry_price, exit_price
    half = (bps / 2.0) / 10000.0
    return entry_price * (1 + half), exit_price * (1 - half)
