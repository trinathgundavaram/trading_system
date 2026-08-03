"""Regime-timed strategy (2026-07-28) - Trinath's ask after docs/
beating_the_market_analysis_2026-07-28.md sec 7g's full 9-config sweep
confirmed every backtest-only entry filter tried (tier, BULL/BEAR exclusion,
VIX cap, price-derived realized-vol cap, 200-day SPY trend) failed to beat
SPY, several actively worse than doing nothing: "come up with a whole new
strategy that can beat SPY... same framework but different rules."

THE ONE THING NONE OF THOSE NINE CONFIGS EVER DID: force-close a position
that was ALREADY OPEN when the market turned. Every filter in sec 7g only
gated NEW entries - once a trade was on, it rode out the real stop machine's
own exit logic (stop/target/time) no matter what SPY did afterward. That's
documented as the likely reason the 200-day trend filter still failed 2022:
it blocked most new entries during the downtrend, but the ~39 that snuck
through (SPY briefly poking back above its 200-SMA mid-bear-market, i.e. a
dead-cat bounce) were free to ride all the way down with no further defense.

THE NEW MECHANISM this module adds, different in KIND, not degree:
  1. compute_regime_timeline() - a SPY trend read (price vs its own SMA)
     with a HYSTERESIS BAND, so a whipsaw right at the line doesn't flip the
     regime state every few days. Classic systematic-trading trend-timing
     technique (see e.g. Faber's 10-month-SMA timing model) - simple,
     explainable, not curve-fit to this specific dataset's quirks.
  2. simulate_forward_exit_regime_gated() - identical to
     engine.backtest_engine.simulate_forward_exit()'s real stop-machine
     replay (same production engine/stop_state_machine.py calls, same
     point-in-time stop-carry discipline), with exactly one addition: if the
     regime read as of the PRIOR trading day's close says risk-off, the
     position is FORCE-CLOSED at today's open, before the stop/target check
     even runs. This is what sec 7g's filters never did. Recorded with
     exit_reason="rule_exit" (an existing member of rules/common.py's closed
     EXIT_KINDS vocabulary - "a sell_rules signal that is none of the
     above" - rather than inventing a new string outside it) plus an
     explicit regime_forced_exit=True flag on the trade dict.
  3. run_regime_timed_replay() - the same real entry pipeline
     (rules/hard_vetoes.py's check(), rules/swing_buy_rules.py's score(),
     same point-in-time TickerData/market-data construction) as
     engine/backtest_engine.py's run_replay(), reused via `import
     engine.backtest_engine as be` rather than copied, so stock SELECTION is
     completely unchanged from production. The only two differences: new
     entries also require yesterday's regime read to be risk-on, and exits
     go through the regime-gated simulator above. This isolates the
     regime-timing question from the stock-picking question on purpose -
     Trinath's own scoping choice ("regime-timing overlay", not "redesigned
     stock-picking logic").

HONEST FRAMING, stated up front rather than left implicit: no real
systematic strategy beats a benchmark in literally every calendar year -
that is not a bar any legitimate backtest should claim to clear, and this
module does not claim to either. What a trend-timing overlay WITH FORCED
EXITS can plausibly do, based on decades of published research on this
exact technique (not this codebase's own results, which are what the sweep
below will show): meaningfully shrink max drawdown during sustained
downtrends like 2022, at some cost to upside capture during fast, choppy
recoveries (a hysteresis band and forced exits both cost you some of the
snapback rally before the trend signal re-confirms). Whether THIS specific
implementation, on THIS universe/window, does that well enough to be worth
running is exactly what needs a real (network-connected) backtest to answer
- see docs/beating_the_market_analysis_2026-07-28.md sec 9.

SCOPE, same boundary every other addition this session has held to
(2026-07-28's AskUserQuestion: "Backtest-only new module (Recommended)"):
this does NOT touch rules/swing_buy_rules.py, engine/regime_engine.py, or
any live/paper trading path. It is a new, separate research module reusing
real production functions where the two questions (stock selection vs
regime timing) can be cleanly reused, without ever writing to them.
"""
import bisect
import logging

import pandas as pd

from engine import execution_costs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Regime timeline: SPY trend vs its own SMA, with a hysteresis deadband
# ---------------------------------------------------------------------------

def compute_regime_timeline(spy_bars: pd.DataFrame, sma_period: int = 200,
                             buffer_pct: float = 3.0) -> dict:
    """Returns {date: bool}, True = risk-on. Stateful walk forward, using
    only each day's OWN close and its own trailing SMA (no look-ahead within
    this function itself - see _regime_asof_yesterday for how CALLERS avoid
    same-day leakage when using this to gate a decision).

    HYSTERESIS: state only flips when price clears the band by buffer_pct in
    EITHER direction (risk-off requires price < sma*(1-buffer), risk-on
    requires price > sma*(1+buffer)); inside the band, the state holds
    whatever it already was. Without this, a raw "price vs SMA" filter
    flips every time price crosses the line, generating exactly the kind of
    thrash a trend filter exists to avoid. buffer_pct=0 reduces to a raw
    crossing test, useful for an A/B comparison of whether the band itself
    is earning its keep.

    Before there's `sma_period` days of history, the state defaults to
    risk-on and simply carries forward once real data arrives - matches this
    codebase's existing "don't fake a filter decision, but also don't
    silently block everything during warmup" posture (compare
    build_market_data_asof's own neutral-default fallbacks)."""
    closes = spy_bars["close"]
    sma = closes.rolling(sma_period).mean()
    timeline = {}
    state = True
    for i in range(len(spy_bars)):
        d = spy_bars["date"].iloc[i]
        s = sma.iloc[i]
        if pd.isna(s):
            timeline[d] = state
            continue
        price = float(closes.iloc[i])
        upper = s * (1 + buffer_pct / 100.0)
        lower = s * (1 - buffer_pct / 100.0)
        if price > upper:
            state = True
        elif price < lower:
            state = False
        # else: inside the deadband - hold the previous state on purpose.
        timeline[d] = state
    return timeline


def _regime_asof_yesterday(spy_dates_sorted: list, regime_by_date: dict, target_date) -> bool:
    """The regime state as of the most recent SPY close STRICTLY BEFORE
    target_date - what a real trader could actually have known before
    target_date's market open. Never reads target_date's own SPY bar. Uses
    bisect over SPY's own trading calendar rather than assuming a ticker's
    bars line up 1:1 with SPY's (holidays/data gaps aside, this is the
    robust way to do it either way).

    No prior SPY history at all (e.g. querying a date before the fetch
    window even starts) defaults to risk-on - see compute_regime_timeline's
    own pre-warmup default for the same reasoning."""
    idx = bisect.bisect_left(spy_dates_sorted, target_date)
    if idx == 0:
        return True
    prior_date = spy_dates_sorted[idx - 1]
    return regime_by_date[prior_date]


# ---------------------------------------------------------------------------
# 2. Regime-gated forward-exit simulation - mirrors backtest_engine's
#    simulate_forward_exit() line for line, with one addition (the regime
#    check). Kept as a separate function rather than adding an optional
#    parameter to the original, so the original (used by every other config
#    in this codebase, including live-adjacent tests) is never at risk of a
#    regression from this experiment.
# ---------------------------------------------------------------------------

def simulate_forward_exit_regime_gated(ticker_bars: pd.DataFrame, entry_idx: int, entry_price: float,
                                        atr: float, pct_score: float, stop_loss_swing_pct: float,
                                        r_multiple: float = 3.0, max_hold_days: int = 20,
                                        cfg: dict = None, atrs: list = None,
                                        spy_dates_sorted: list = None, regime_by_date: dict = None) -> dict:
    """Real production stop machine (engine/stop_state_machine.py's
    calculate()/should_advance(), identical to backtest_engine.
    simulate_forward_exit()), plus ONE new check at the top of each bar: if
    _regime_asof_yesterday says risk-off, force-close at THIS bar's OPEN
    with exit_reason="regime_exit", before the stop/target test even runs.
    spy_dates_sorted/regime_by_date both None reproduces the plain
    stop/target/time-stop behavior exactly (no regime gating at all) - used
    by this module's own tests to confirm zero behavioral drift when the
    new mechanism is switched off."""
    from engine import stop_state_machine as ssm
    from engine.backtest_engine import _exit_result, _entry_tier
    from rules.common import EXIT_KINDS

    assert "rule_exit" in EXIT_KINDS, "rules/common.py's EXIT_KINDS vocabulary changed shape"
    cfg = cfg or {}
    n = len(ticker_bars)
    entry_atr = atr if atr and atr > 0 else entry_price * 0.015

    def _atr_at(i):
        if atrs is not None and 0 <= i < len(atrs) and atrs[i]:
            return float(atrs[i])
        return entry_atr

    position = {
        "entry_price": entry_price,
        "entry_signal_score": pct_score,
        "trade_mode": "SWING",
        "high_watermark_price": entry_price,
        "stop_state": None,
    }
    td0 = {"price": entry_price, "atr": entry_atr}
    initial = ssm.calculate(position, td0, 0.0, cfg)
    stop_price = initial.stop_price
    stop_state = initial.state
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        risk_per_share = entry_price * (stop_loss_swing_pct / 100.0)
        stop_price = entry_price - risk_per_share
    position["risk_per_share"] = risk_per_share
    position["stop_state"] = stop_state.value
    target_price = entry_price + risk_per_share * r_multiple

    tier = _entry_tier(pct_score)
    mae_pct, mfe_pct = 0.0, 0.0
    high_watermark = entry_price
    last_idx = entry_idx

    for i in range(entry_idx + 1, min(entry_idx + 1 + max_hold_days, n)):
        bar = ticker_bars.iloc[i]
        bar_date = bar["date"]
        open_ = float(bar["open"])
        low_, high_, close_ = float(bar["low"]), float(bar["high"]), float(bar["close"])
        mae_pct = min(mae_pct, (low_ / entry_price - 1) * 100)
        mfe_pct = max(mfe_pct, (high_ / entry_price - 1) * 100)
        last_idx = i

        # NEW: regime gate, checked BEFORE the stop/target test - see module
        # docstring. Takes priority: a bear-market force-exit isn't supposed
        # to wait around for the stop to also catch up. Uses "rule_exit" -
        # an existing EXIT_KINDS member ("a sell_rules signal that is none
        # of the above") - rather than a brand-new string outside this
        # codebase's closed exit vocabulary; nothing else in THIS specific
        # exit simulator ever produces "rule_exit", so it unambiguously
        # means a regime-forced exit here. regime_forced_exit=True is the
        # explicit flag for this module's own telemetry/tests to key off,
        # rather than relying on that coincidence.
        if spy_dates_sorted is not None and regime_by_date is not None:
            if not _regime_asof_yesterday(spy_dates_sorted, regime_by_date, bar_date):
                result = _exit_result(i, open_, entry_price, entry_idx, mae_pct, mfe_pct,
                                      "rule_exit", tier, stop_state=stop_state.value,
                                      risk_per_share=risk_per_share, cfg=cfg)
                result["regime_forced_exit"] = True
                return result

        # 1. Test the stop CARRIED INTO this bar (priced off bar i-1's close)
        #    - identical point-in-time discipline to simulate_forward_exit.
        if low_ <= stop_price:
            return _exit_result(i, stop_price, entry_price, entry_idx, mae_pct, mfe_pct,
                                stop_state.exit_kind, tier, stop_state=stop_state.value,
                                risk_per_share=risk_per_share, cfg=cfg)
        if high_ >= target_price:
            return _exit_result(i, target_price, entry_price, entry_idx, mae_pct, mfe_pct,
                                "take_profit", tier, stop_state=stop_state.value,
                                risk_per_share=risk_per_share, cfg=cfg)

        # 2. Bar survived - advance the watermark and re-price for the NEXT bar.
        high_watermark = max(high_watermark, high_)
        position["high_watermark_price"] = high_watermark
        candidate = ssm.calculate(position, {"price": close_, "atr": _atr_at(i)}, 0.0, cfg)
        if ssm.should_advance(stop_price, candidate):
            stop_price = candidate.stop_price
            stop_state = candidate.state
            position["stop_state"] = stop_state.value

    exit_price = float(ticker_bars.iloc[last_idx]["close"])
    return _exit_result(last_idx, exit_price, entry_price, entry_idx, mae_pct, mfe_pct,
                        "time_stop", tier, stop_state=stop_state.value,
                        risk_per_share=risk_per_share, cfg=cfg)


# ---------------------------------------------------------------------------
# 3. Full replay - reuses backtest_engine's real entry pipeline via `be.*`
#    rather than copying it, so stock SELECTION is byte-identical to
#    production. Only the regime-timing pieces above are new.
# ---------------------------------------------------------------------------

def run_regime_timed_replay(tickers: list, start: str, end: str, cfg: dict,
                             sma_period: int = 200, buffer_pct: float = 3.0,
                             warmup_days: int = 260, max_hold_days: int = 20,
                             progress_cb=None) -> dict:
    """Same real hard_vetoes.check()/swing_buy_rules.score() pipeline as
    engine.backtest_engine.run_replay() - imported and called, not
    reimplemented. Differences from that function: (1) a new candidate also
    needs yesterday's regime read to be risk-on before it's even scored
    (treated exactly like a sub-threshold score - i advances, the ticker's
    timeline moves on to the next opportunity, same pattern backtest_
    engine's own entry_filter uses); (2) exits go through
    simulate_forward_exit_regime_gated() above instead of the plain
    version, so an already-open position gets force-closed the day after
    the regime flips, not just blocked from opening new ones.

    2026-07-28, same-day fix: this ALSO applies backtest_engine's existing
    cfg["backtest"]["entry_filter"] (min_tier/exclude_regimes/max_vix/etc,
    via entry_filter_from_cfg()/passes_entry_filter()) exactly like
    run_replay() does, stacked alongside the new regime gate - both must
    pass. First version of this function did NOT do this, on the theory
    that isolating the regime-timing mechanism meant testing it against raw
    scoring alone. That produced a badly misleading first sweep: without
    the standard-tier/exclude-BULL filter every other config in docs/
    beating_the_market_analysis_2026-07-28.md sec 7g was built on, trade
    rate matched sec 1's UNFILTERED Run A almost exactly (13.6 vs 14.1
    trades/ticker/year) and the results (CAGR -9% to -15%) were a
    regression to that unfiltered baseline, not a real test of what regime
    timing adds on top of the proven filter. Default entry_filter (empty
    cfg) is unchanged - "off" - callers must opt in the same as run_replay's
    callers do."""
    from rules import hard_vetoes, swing_buy_rules
    from engine import backtest_engine as be

    fetch_start = (pd.to_datetime(start) - pd.Timedelta(days=int(warmup_days * 1.6))).strftime("%Y-%m-%d")
    spy_bars = be.fetch_daily_bars("SPY", fetch_start, end)
    vix_series = be.fetch_vix_series(fetch_start, end)
    vix_by_date = dict(zip(vix_series["date"], vix_series["vix"]))

    regime_by_date = compute_regime_timeline(spy_bars, sma_period=sma_period, buffer_pct=buffer_pct)
    spy_dates_sorted = sorted(regime_by_date.keys())

    risk_level = cfg.get("risk_level", "MODERATE")
    base_threshold = (cfg.get("risk", {}).get(risk_level, {}) or {}).get("buy_score_threshold_pct", 60)
    stop_loss_swing_pct = (cfg.get("risk", {}).get(risk_level, {}) or {}).get("stop_loss_swing_pct", 5.0)
    r_multiple = (cfg.get("sell_rules", {}).get("take_profit", {}) or {}).get("r_multiple", 3.0)
    entry_filter = be.entry_filter_from_cfg(cfg)

    open_positions: dict = {}
    original_db = be._patch_database(open_positions)
    original_breadth = be._patch_market_breadth()
    all_trades = []
    veto_counts: dict = {}
    n_scored = 0
    n_regime_blocked_entries = 0
    n_entry_filter_rejected = 0

    try:
        for t_idx, ticker in enumerate(tickers, start=1):
            if progress_cb is not None:
                try:
                    progress_cb(t_idx, len(tickers), ticker)
                except Exception:
                    pass
            try:
                bars = be.fetch_daily_bars(ticker, fetch_start, end)
            except Exception as e:
                logger.warning(f"{ticker}: skipped, bar fetch failed ({e})")
                continue

            bar_atrs = be.atr_series(bars)
            start_date = pd.to_datetime(start).date()
            start_idx = int((bars["date"] >= start_date).idxmax()) if (bars["date"] >= start_date).any() else None
            if start_idx is None or start_idx < 20:
                continue

            i = start_idx
            n = len(bars)
            open_positions.pop(ticker, None)
            while i < n:
                asof_date = bars["date"].iloc[i]
                window = bars.iloc[: i + 1]
                spy_window = spy_bars[spy_bars["date"] <= asof_date]
                if len(spy_window) < 50:
                    i += 1
                    continue
                vix_close = vix_by_date.get(asof_date)

                # NEW: a candidate that would otherwise be scored is instead
                # skipped entirely if yesterday's regime read is risk-off -
                # same "advance i, move to the next opportunity" pattern as
                # backtest_engine's entry_filter, not a post-hoc slice.
                if not _regime_asof_yesterday(spy_dates_sorted, regime_by_date, asof_date):
                    n_regime_blocked_entries += 1
                    i += 1
                    continue

                td = be.build_ticker_data_asof(ticker, window)
                ticker_dict = be._td_to_dict(td)
                market_dict = be.build_market_data_asof(spy_window, vix_close, asof_date)
                regime = be.compute_regime_asof(spy_window, vix_close)

                veto = hard_vetoes.check(ticker, ticker_dict, market_dict, cfg, mode="swing")
                if veto.vetoed:
                    veto_counts[veto.veto_code] = veto_counts.get(veto.veto_code, 0) + 1
                    i += 1
                    continue

                n_scored += 1
                result = swing_buy_rules.score(ticker_dict, market_dict, regime, cfg, mode="swing",
                                                db=None, ticker=ticker)
                if not result.passed:
                    i += 1
                    continue

                # 2026-07-28 fix: stack the SAME backtest-only entry_filter
                # run_replay() supports (min_tier/exclude_regimes/max_vix/
                # max_realized_vol_pct/min_spy_trend_pct) - see this
                # function's docstring for why leaving this out on the
                # first pass made the sweep misleading. Only computed when
                # the filter actually uses it, same conditional-cost
                # posture as run_replay's own version.
                rv = (be.realized_vol_pct(spy_window)
                      if entry_filter.get("max_realized_vol_pct") is not None else None)
                spy_trend = (be.spy_trend_pct(spy_window)
                             if entry_filter.get("min_spy_trend_pct") is not None else None)
                if not be.passes_entry_filter(
                        be._entry_tier(result.final_score_pct), regime.dominant_regime, entry_filter,
                        vix=vix_close, realized_vol_pct_value=rv, spy_trend_pct_value=spy_trend):
                    n_entry_filter_rejected += 1
                    i += 1
                    continue

                entry_price = td.price
                exit_info = simulate_forward_exit_regime_gated(
                    bars, i, entry_price, td.atr, result.final_score_pct,
                    stop_loss_swing_pct, r_multiple, max_hold_days,
                    cfg=cfg, atrs=bar_atrs,
                    spy_dates_sorted=spy_dates_sorted, regime_by_date=regime_by_date,
                )
                trade = {
                    "ticker": ticker,
                    "entry_date": str(asof_date),
                    "entry_price": round(entry_price, 4),
                    "exit_date": str(bars["date"].iloc[exit_info["exit_idx"]]),
                    "buy_score_pct": round(result.final_score_pct, 2),
                    "threshold_pct": round(result.threshold, 2),
                    "regime": regime.dominant_regime,
                    "rules_fired": result.rules_fired,
                    **exit_info,
                }
                all_trades.append(trade)
                i = exit_info["exit_idx"] + 1
    finally:
        be._unpatch_database(original_db)
        be._unpatch_market_breadth(original_breadth)

    return {
        "trades": all_trades,
        "n_scored": n_scored,
        "veto_counts": veto_counts,
        "summary": be.summarize(all_trades),
        "n_regime_blocked_entries": n_regime_blocked_entries,
        "n_regime_forced_exits": sum(1 for t in all_trades if t.get("regime_forced_exit")),
        "n_entry_filter_rejected": n_entry_filter_rejected,
        "config": {
            "risk_level": risk_level, "base_threshold_pct": base_threshold,
            "stop_loss_swing_pct": stop_loss_swing_pct, "r_multiple": r_multiple,
            "mode": "swing", "max_hold_days": max_hold_days,
            "sma_period": sma_period, "buffer_pct": buffer_pct,
            "execution_cost_bps": execution_costs.get_round_trip_bps(cfg),
            "entry_filter": {
                "min_tier": entry_filter["min_tier"],
                "exclude_regimes": sorted(entry_filter["exclude_regimes"]),
                "max_vix": entry_filter.get("max_vix"),
                "max_realized_vol_pct": entry_filter.get("max_realized_vol_pct"),
                "min_spy_trend_pct": entry_filter.get("min_spy_trend_pct"),
            },
        },
    }
