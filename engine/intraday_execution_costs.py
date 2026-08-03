"""Per-share commission+slippage execution cost model for the intraday
momentum backtest (2026-07-30, phase 1 of docs/
intraday_momentum_scoping_2026-07-30.md).

WHY A SEPARATE MODEL FROM engine/execution_costs.py: that module haircuts a
flat round-trip basis-points figure, the right unit for the daily swing
engine where position size/ticker varies a lot. This strategy trades one
instrument (SPY) at much higher frequency, and the paper's own disclosed
cost assumption is a fixed $/share amount, not a %. A $/share cost expressed
as a % of the entry price is independent of position size (buying 1 share
or 1000 costs the same $/share on each), so this module works directly in
$/share terms and lets callers convert to % against whatever price a trade
used - no notional/share-count needed for that conversion.

DEFAULT is the paper's own disclosed assumption ($0.0035/share commission +
$0.001/share slippage = $0.0045/share, charged on EACH fill - once on
entry, once on exit) - a deliberate departure from this codebase's usual
"ship new cost knobs default-OFF" convention (see engine/execution_costs.py
DEFAULT_ROUND_TRIP_BPS=0.0). The whole point of phase 1 is testing whether
the paper's own reported numbers survive the paper's own disclosed
friction, so defaulting this to zero would silently answer a different,
easier question. Pass commission=0.0, slippage=0.0 explicitly for a
frictionless comparison run.
"""

DEFAULT_PER_SHARE_COMMISSION = 0.0035
DEFAULT_PER_SHARE_SLIPPAGE = 0.001


def round_trip_cost_per_share(commission: float = DEFAULT_PER_SHARE_COMMISSION,
                               slippage: float = DEFAULT_PER_SHARE_SLIPPAGE) -> float:
    """Cost charged on ONE fill (entry OR exit), never negative regardless
    of what's passed in - a cost model that could go negative would be
    paying the trader to trade, which is not a friction assumption worth
    silently allowing."""
    return max(0.0, commission) + max(0.0, slippage)


def get_cost_params(cfg: dict) -> tuple:
    """Reads intraday_backtest.commission_per_share / .slippage_per_share
    from cfg, falling back to the paper's own defaults above (NOT to zero -
    see module docstring for why this model's default posture is inverted
    from execution_costs.py's). Tolerant of a missing/malformed config the
    same way get_round_trip_bps() is - a bad value degrades to the default
    cost assumption, not a crash or a silent zero."""
    cfg = cfg or {}
    ib_cfg = cfg.get("intraday_backtest", {}) or {}
    try:
        commission = float(ib_cfg.get("commission_per_share", DEFAULT_PER_SHARE_COMMISSION))
    except (TypeError, ValueError):
        commission = DEFAULT_PER_SHARE_COMMISSION
    try:
        slippage = float(ib_cfg.get("slippage_per_share", DEFAULT_PER_SHARE_SLIPPAGE))
    except (TypeError, ValueError):
        slippage = DEFAULT_PER_SHARE_SLIPPAGE
    return commission, slippage


def apply_execution_costs(entry_price: float, exit_price: float, direction: str,
                           commission: float = DEFAULT_PER_SHARE_COMMISSION,
                           slippage: float = DEFAULT_PER_SHARE_SLIPPAGE) -> dict:
    """Returns raw and cost-adjusted P&L per share, and both as a % of
    entry price (the basis every other outcome_pct in this codebase uses).

    direction: 'long' or 'short'. Cost is symmetric - charged once per
    fill, twice per round trip, regardless of direction or whether the
    trade won or lost (a real broker/ECN charges commission either way;
    this is friction, not a P&L-dependent adjustment)."""
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if not entry_price or not exit_price:
        raise ValueError("entry_price and exit_price must both be positive")
    per_fill = round_trip_cost_per_share(commission, slippage)
    raw_pnl_per_share = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
    cost_per_share_round_trip = 2 * per_fill
    net_pnl_per_share = raw_pnl_per_share - cost_per_share_round_trip
    return {
        "raw_pnl_per_share": raw_pnl_per_share,
        "net_pnl_per_share": net_pnl_per_share,
        "cost_per_share_round_trip": cost_per_share_round_trip,
        "raw_outcome_pct": (raw_pnl_per_share / entry_price) * 100,
        "net_outcome_pct": (net_pnl_per_share / entry_price) * 100,
    }
