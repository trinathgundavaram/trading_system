#!/usr/bin/env python3
"""Compounded equity-curve simulator (2026-07-28) - the gap flagged in
docs/beating_the_market_analysis_2026-07-28.md sec 5/6: profit_factor,
win_rate and expectancy_r all measure per-trade QUALITY, not portfolio
RETURN. "What's the return % per year" cannot be read off those numbers
directly - it needs a position-sizing rule and a compounding simulation,
which is what this script does, entirely as post-processing on an existing
results.json (no new backtest run or network access needed).

METHOD - honest about what this is and isn't:
  - Trades are replayed as (open, close) events in chronological order.
  - At OPEN, the position's dollar RISK is `risk_per_trade_pct * equity_at_
    that_moment` (equity compounds - a new trade sizes off whatever the
    portfolio has grown/shrunk to by ITS entry date). The position's dollar
    VALUE (what a real account must actually pay to buy the shares) is
    derived from that risk and the trade's own stop distance:
    `position_value = dollar_risk / (risk_pct / 100)` - e.g. risking $1,000
    with a 5%-away stop means committing $20,000 of capital, not $1,000.
  - CAPITAL CONSTRAINT (2026-07-28, Trinath: "do not test for unlimited
    cash... go for a fixed start capital and continue with that portfolio").
    A running `cash` balance starts at starting_equity and is debited
    position_value at every OPEN, credited position_value + realized P&L at
    every CLOSE. If a candidate's position_value exceeds AVAILABLE cash at
    its open, the trade is REJECTED - not resized, not queued, just skipped
    entirely, the way a real account with genuinely insufficient buying
    power would miss that setup. This is deliberately the strict version:
    a real broker might extend margin or let you take a smaller size instead
    of a hard no - simulating that would need an assumption about margin
    terms this doesn't have, so "skip" is the conservative default. Rejected
    trades are counted (n_capital_constrained_rejected) so the report shows
    how often finite capital actually bound.
  - At CLOSE, P&L is realized as `dollar_risk * outcome_r` (outcome_r is
    already expressed in multiples of that trade's own risk - see
    engine/backtest_engine.py's _exit_result). `equity` (used for CAGR/
    Sharpe/drawdown) only moves at realized P&L, same as before - it is
    NOT the same figure as `cash` (equity = cash + capital currently
    deployed in open positions, carried at cost, not marked-to-market
    between open and close - this replay has no daily price path to mark
    against, only entry/exit prices).
  - `--unlimited-capital` reproduces the OLD (2026-07-28 morning) behaviour
    for comparison only - every candidate allowed to open regardless of
    available cash. That number is an upper bound on what infinite capital
    could achieve, explicitly NOT the default anymore.
  - Trades missing outcome_r or risk_pct (no risk/sizing basis recorded) are
    skipped from the equity simulation but counted in the report so n stays
    honest.

SIZING MODE (2026-07-28 evening addendum, sec 9e - the sequence-of-returns
fix). `docs/beating_the_market_analysis_2026-07-28.md` sec 9d's blind
walk-forward found PF=1.17 (trade-level profitable) alongside CAGR=-5.1%
(compounded loss) on the same trade sequence - a real, verifiable signature
of sequence-of-returns risk under the DEFAULT sizing rule above: risking a
fixed % of CURRENT equity means a losing stretch shrinks every subsequent
position size, so later winning trades recover fewer dollars than the
earlier losing trades cost, even when the later trades are individually
just as good. `--sizing-mode high_watermark` is an alternative: positions
size off the HIGHEST equity level reached so far (never decreases), not
current equity. During a drawdown this means betting the SAME dollar amount
as at the last peak rather than a shrunk one - the "de-risking exactly when
you need to recover" trap current_equity sizing can fall into, at the
explicit cost of risking a LARGER percentage of actual current capital
during that drawdown (a real, not-free tradeoff - see this script's own
tests for a worked comparison). Default stays `current_equity` - this is an
experiment, not a proven fix; see sec 9e for whether it actually helped on
the specific sequence that motivated it.

SHARPE - also honest about its limits. This backtest only marks equity at
each trade CLOSE (no daily mark-to-market), so a true daily Sharpe isn't
available. This computes month-end equity by forward-filling the last known
close-event value into every calendar month, then takes month-over-month
returns and annualizes with sqrt(12). Months with no trade activity show a
flat (0%) return, which understates real volatility slightly (a live account
holding open positions would show intra-month mark-to-market swings this
trade-close-only replay doesn't capture) - so treat this Sharpe as an upper
bound on smoothness, same "not a validated live number" caveat as the CAGR
figures. Risk-free rate defaults to 0 (--risk-free-rate-annual to change).

Usage:
    python3 scripts/equity_curve.py output/backtest_results/<ts>/results.json
    python3 scripts/equity_curve.py output/backtest_results/<ts>/results.json --risk-pct 1.0
    python3 scripts/equity_curve.py output/backtest_results/<ts>/results.json --risk-pct 0.5 1.0 2.0
    python3 scripts/equity_curve.py output/backtest_results/<ts>/results.json --compare-spy
"""
import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path


def _month_ends(start_dt: date, end_dt: date) -> list:
    ends = []
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        last_day = calendar.monthrange(y, m)[1]
        ends.append(date(y, m, last_day))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return ends


def _sharpe(curve: list, start_dt: date, end_dt: date, starting_equity: float,
            risk_free_rate_annual: float) -> dict:
    """curve: [(date_str, equity), ...] from simulate(), already sorted by
    date. Forward-fills month-end equity, computes monthly returns, and
    annualizes. Returns None fields if there's under 3 months of data (a
    Sharpe on 1-2 points is noise, not a number)."""
    events = sorted((date.fromisoformat(d), eq) for d, eq in curve)
    month_ends = _month_ends(start_dt, end_dt)
    monthly_equity = []
    idx = 0
    last_eq = starting_equity
    for me in month_ends:
        while idx < len(events) and events[idx][0] <= me:
            last_eq = events[idx][1]
            idx += 1
        monthly_equity.append(last_eq)

    if len(monthly_equity) < 4:
        return {"sharpe": None, "n_months": len(monthly_equity),
                "note": "fewer than 4 months of data - Sharpe not meaningful"}

    returns = [(monthly_equity[i] / monthly_equity[i - 1] - 1)
               for i in range(1, len(monthly_equity)) if monthly_equity[i - 1] > 0]
    if len(returns) < 3:
        return {"sharpe": None, "n_months": len(monthly_equity),
                "note": "fewer than 3 return observations - Sharpe not meaningful"}

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    std_r = variance ** 0.5
    monthly_rf = (1 + risk_free_rate_annual / 100.0) ** (1 / 12) - 1
    if std_r == 0:
        return {"sharpe": None, "n_months": len(monthly_equity),
                "note": "zero return variance - Sharpe undefined"}
    sharpe = (mean_r - monthly_rf) / std_r * (12 ** 0.5)
    return {"sharpe": round(sharpe, 2), "n_months": len(monthly_equity), "note": None}


def simulate(trades: list, risk_pct: float, starting_equity: float = 100_000.0,
             risk_free_rate_annual: float = 0.0, unlimited_capital: bool = False,
             sizing_mode: str = "current_equity", capital_mode: str = "reject") -> dict:
    """capital_mode (2026-07-31, sec 9e follow-up - "next step would be a
    partial-resize capital model instead of hard rejection"):

      - "reject" (default, unchanged behavior): a candidate whose desired
        position VALUE exceeds available cash is skipped entirely - the
        original 2026-07-28-afternoon model this whole module was built
        around ("do not test for unlimited cash... go for a fixed start
        capital"), still the default so nothing existing silently changes.
      - "partial_resize": the same candidate is NOT skipped. Instead it
        opens at whatever SMALLER size fits in the cash actually available
        (position_value capped at cash, dollar_risk scaled down in the
        same proportion, since risk_pct is a property of the trade's own
        stop distance and doesn't change just because size did). This is
        what sec 9e's real validation run found missing: under "reject",
        high_watermark sizing's whole point (bet the SAME size during a
        drawdown, not a shrunk one) was being defeated by a blunter
        capital rule that just skipped the oversized bet instead of taking
        a smaller version of it - measured result was high_watermark
        UNDERPERFORMING current_equity (1.3% vs 9.9% CAGR) under "reject",
        the opposite of what the mechanism is FOR, while high_watermark
        clearly won under --unlimited-capital (24.0% vs 20.5%) once the
        capital constraint was removed entirely - confirming the sizing
        mechanism itself works and the "reject" capital rule was what was
        actually fighting it. partial_resize is the middle ground: still a
        genuine fixed-capital constraint (nothing opens beyond what cash
        allows, unlike --unlimited-capital), but a good signal that can't
        fit at full size still gets SOME exposure instead of none.
      A trade resized this way can end up very small if cash is nearly
      exhausted (no minimum position-value floor is enforced) - n_resized
      and the smallest resized position's value are both reported so this
      is visible rather than silently blended into the other numbers."""
    if sizing_mode not in ("current_equity", "high_watermark"):
        raise ValueError(f"sizing_mode must be 'current_equity' or 'high_watermark', got {sizing_mode!r}")
    if capital_mode not in ("reject", "partial_resize"):
        raise ValueError(f"capital_mode must be 'reject' or 'partial_resize', got {capital_mode!r}")
    events = []
    skipped = 0
    for t in trades:
        if t.get("outcome_r") is None or not t.get("risk_pct"):
            skipped += 1
            continue
        events.append((t["entry_date"], "open", t))
        events.append((t["exit_date"], "close", t))
    # opens before closes on the same date (a trade cannot close before it
    # opens, and this keeps same-day round-trips sane). Ties beyond that
    # keep the source list's original order - deterministic, not bias-free,
    # but there is no ground truth for intraday ordering in a daily-bar
    # replay either way.
    events.sort(key=lambda e: (e[0], 0 if e[1] == "open" else 1))

    equity = starting_equity
    cash = starting_equity
    dollar_risk = {}     # trade id -> dollars at risk (committed at open)
    position_value = {}  # trade id -> dollars of capital deployed (committed at open)
    opened = set()        # trade ids that actually opened (vs. capital-rejected)
    peak = equity
    max_drawdown_pct = 0.0
    curve = []  # (date, equity) after each close, for optional inspection
    n_rejected = 0
    n_resized = 0
    smallest_resized_pv = None

    for dt, kind, t in events:
        key = id(t)
        if kind == "open":
            sizing_base = peak if sizing_mode == "high_watermark" else equity
            risked = sizing_base * (risk_pct / 100.0)
            # trade's own risk_pct is the % distance from entry to stop -
            # the capital needed to buy a position sized so that a full
            # stop-out loses exactly `risked` dollars.
            pv = risked / (t["risk_pct"] / 100.0)
            if not unlimited_capital and pv > cash:
                if capital_mode == "reject" or cash <= 0:
                    n_rejected += 1
                    continue
                # partial_resize: take whatever cash IS available instead
                # of skipping - scale dollar_risk down in the same
                # proportion pv itself was scaled down, so risk_pct (the
                # trade's own stop-distance property) stays internally
                # consistent with the smaller position actually taken.
                pv = cash
                risked = pv * (t["risk_pct"] / 100.0)
                n_resized += 1
                smallest_resized_pv = pv if smallest_resized_pv is None else min(smallest_resized_pv, pv)
            dollar_risk[key] = risked
            position_value[key] = pv
            opened.add(key)
            if not unlimited_capital:
                cash -= pv
        else:
            if key not in opened:
                continue  # never opened (capital-rejected) - nothing to close
            risked = dollar_risk.pop(key, 0.0)
            pv = position_value.pop(key, 0.0)
            opened.discard(key)
            pnl = risked * t["outcome_r"]
            equity += pnl
            if not unlimited_capital:
                cash += pv + pnl
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
            max_drawdown_pct = max(max_drawdown_pct, dd)
            curve.append((dt, round(equity, 2)))

    if not curve:
        return {"risk_pct": risk_pct, "n_trades_simulated": 0, "skipped_no_risk_basis": skipped,
                "n_capital_constrained_rejected": n_rejected, "n_resized": n_resized,
                "sizing_mode": sizing_mode, "capital_mode": capital_mode}

    start_dt = date.fromisoformat(min(t["entry_date"] for t in trades if t.get("outcome_r") is not None))
    end_dt = date.fromisoformat(max(t["exit_date"] for t in trades if t.get("outcome_r") is not None))
    years = max((end_dt - start_dt).days / 365.25, 0.01)
    total_return_pct = (equity / starting_equity - 1) * 100
    cagr_pct = ((equity / starting_equity) ** (1 / years) - 1) * 100
    sharpe_info = _sharpe(curve, start_dt, end_dt, starting_equity, risk_free_rate_annual)

    return {
        "risk_pct": risk_pct,
        "n_trades_simulated": len(curve),
        "skipped_no_risk_basis": skipped,
        "n_capital_constrained_rejected": n_rejected,
        "n_resized": n_resized,
        "smallest_resized_position_value": round(smallest_resized_pv, 2) if smallest_resized_pv is not None else None,
        "years": round(years, 2),
        "starting_equity": starting_equity,
        "final_equity": round(equity, 2),
        "total_return_pct": round(total_return_pct, 1),
        "cagr_pct": round(cagr_pct, 1),
        "max_drawdown_pct": round(max_drawdown_pct, 1),
        "sharpe": sharpe_info["sharpe"],
        "sharpe_note": sharpe_info["note"],
        "sizing_mode": sizing_mode,
        "capital_mode": capital_mode,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_json", help="Path to a backtest results.json")
    ap.add_argument("--risk-pct", type=float, nargs="+", default=[0.5, 1.0, 2.0],
                     help="Fraction of current equity risked per trade (default: 0.5, 1.0, 2.0 for "
                          "a sensitivity read - real professional guidance is rarely above 1-2%%)")
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--risk-free-rate-annual", type=float, default=0.0,
                     help="Annual risk-free rate %% for the Sharpe calc (default: 0.0 - a real comparison "
                          "should use a T-bill rate for the same window, e.g. ~4-5%% recently)")
    ap.add_argument("--unlimited-capital", action="store_true",
                     help="Reproduce the OLD (pre-2026-07-28-afternoon) behaviour: every candidate opens "
                          "regardless of available cash. An upper bound for comparison only - NOT the "
                          "default, since a real account has a fixed starting balance.")
    ap.add_argument("--sizing-mode", choices=["current_equity", "high_watermark"], default="current_equity",
                     help="current_equity (default): risk %% of equity AS OF each trade's own open - can "
                          "shrink position sizes during a drawdown, the sequence-of-returns risk sec 9d "
                          "found. high_watermark: risk %% of the HIGHEST equity reached so far - does not "
                          "shrink during a drawdown, at the cost of risking more of actual current capital "
                          "while underwater. See this script's module docstring.")
    ap.add_argument("--capital-mode", choices=["reject", "partial_resize"], default="reject",
                     help="reject (default, unchanged): a candidate that doesn't fit in available cash is "
                          "skipped entirely. partial_resize (sec 9e follow-up): the same candidate opens "
                          "at whatever smaller size DOES fit, instead of being skipped - see simulate()'s "
                          "docstring for why 'reject' was found to be actively fighting high_watermark "
                          "sizing's whole point. No effect under --unlimited-capital (nothing is ever "
                          "capital-constrained there to reject OR resize).")
    ap.add_argument("--compare-spy", action="store_true",
                     help="Also fetch and print SPY's buy-and-hold CAGR over the same window "
                          "(requires network/yfinance - see scripts/spy_benchmark.py)")
    args = ap.parse_args()

    d = json.loads(Path(args.results_json).read_text())
    trades = d.get("trades", [])
    cfg = d.get("config", {})
    print(f"{args.results_json}")
    print(f"  entry_filter={cfg.get('entry_filter')}  stop_loss_swing_pct={cfg.get('stop_loss_swing_pct')}  "
          f"execution_cost_bps={cfg.get('execution_cost_bps')}  n_trades={len(trades)}")
    print()
    if args.unlimited_capital:
        print("MODE: --unlimited-capital - every candidate opens regardless of available cash. "
              "This is an upper bound for comparison, NOT a validated live-tradeable return.")
    elif args.capital_mode == "partial_resize":
        print(f"MODE: fixed starting capital (${args.starting_equity:,.0f}) - a candidate that doesn't fit "
              "in available cash opens at whatever SMALLER size does fit (partial_resize - see this "
              "script's module docstring). Watch the Resized column: a high count means many trades are "
              "running below their intended size, not that they're missing entirely.")
    else:
        print(f"MODE: fixed starting capital (${args.starting_equity:,.0f}) - a candidate whose position "
              "size exceeds available cash at open is REJECTED, not resized (see this script's module "
              "docstring). Watch the Rejected column: a high count means capital, not signal quality, is "
              "the binding constraint at that risk-pct.")
    print(f"SIZING: {args.sizing_mode}"
          + (" - risks %% of equity AS OF each trade's open." if args.sizing_mode == "current_equity"
             else " - risks %% of the HIGHEST equity reached so far, not current equity (sec 9e experiment)."))
    print("Sharpe is from month-end resampled equity (trade-close marks only), not true daily mark-to-market.")
    print()
    print(f"{'Risk/trade':>12} {'Years':>7} {'CAGR':>8} {'TotalReturn':>13} {'MaxDD':>8} {'Sharpe':>8} "
          f"{'Trades':>8} {'Rejected':>9} {'Resized':>8}")
    for rp in args.risk_pct:
        r = simulate(trades, rp, args.starting_equity, args.risk_free_rate_annual,
                     unlimited_capital=args.unlimited_capital, sizing_mode=args.sizing_mode,
                     capital_mode=args.capital_mode)
        if r.get("n_trades_simulated", 0) == 0:
            print(f"{rp:>11}% - no trades with a risk basis to simulate")
            continue
        sharpe_str = f"{r['sharpe']}" if r["sharpe"] is not None else f"n/a ({r['sharpe_note']})"
        print(f"{rp:>11}% {r['years']:>7} {r['cagr_pct']:>7}% {r['total_return_pct']:>12}% "
              f"{r['max_drawdown_pct']:>7}% {sharpe_str:>8} {r['n_trades_simulated']:>8} "
              f"{r.get('n_capital_constrained_rejected', 0):>9} {r.get('n_resized', 0):>8}")

    if args.compare_spy:
        print()
        try:
            from spy_benchmark import spy_return
            dates = sorted(t["entry_date"] for t in trades)
            bench = spy_return(dates[0], max(t["exit_date"] for t in trades))
            print(f"SPY buy-and-hold, {bench['start']} -> {bench['end']} ({bench['years']} years): "
                  f"total return {bench['total_return_pct']}%, CAGR {bench['cagr_pct']}%")
            print("Compare against the CAGR column above at whichever --risk-pct you actually intend to "
                  "trade at - not the highest one, since higher risk-pct rows above are NOT free money "
                  "(see the max_drawdown column, and note CAGR does not always increase with risk-pct - "
                  "overbetting a middling win rate can make CAGR worse, not better).")
        except Exception as e:
            print(f"SPY comparison failed ({e}) - run scripts/spy_benchmark.py separately if this "
                  "environment lacks network/yfinance access.")
    else:
        print()
        print("Compare CAGR above against SPY's over the same window via scripts/spy_benchmark.py "
              f"--results-json {args.results_json}  (or pass --compare-spy to this script)")


if __name__ == "__main__":
    sys.exit(main())
