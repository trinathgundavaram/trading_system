"""Layer 0: Hard vetoes. Any single veto fires -> SKIP immediately.
Called BEFORE scoring. No scoring runs if vetoed.

Originally 15 vetoes. Veto #5 (BREADTH_PANIC / AD_COLLAPSE) was removed
2026-07-15: single-indicator breadth is too noisy as a per-ticker hard block.
Breadth now flows through the MARKET_BREADTH scoring bucket, dynamic threshold
adjustment, and a MULTI-SIGNAL market gate in rules/market_filters.py. Now 14
active vetoes plus the data-provenance circuit breaker (#16, unchanged).

ticker_data / market_data here are plain dicts produced by
engine/ticker_data_adapter.py (not the raw TickerData/MarketContextData
dataclasses) - several of the fields checked below (news_classified,
options_expected_move_pct, etc.) are still placeholders in that adapter, so
those specific vetoes can never fire yet. See ticker_data_adapter.py for
exactly which fields are real.

avwap_earnings (veto #7, BELOW_AVWAP) went REAL 2026-07-16 (FMP's real
last-earnings-report date, see engine/ticker_analyzer.py's
_calc_earnings_avwap()) - this veto was previously dead code (avwap_earnings
was always 0.0, so the `avwap_earnings > 0` guard always failed) and can now
actually fire and block a buy. Flagged explicitly here because a hard veto
going live is a bigger behavior change than a scoring-bucket rule going
live - watch BELOW_AVWAP in the veto logs for the first cycles after this
deploys."""
from dataclasses import dataclass


@dataclass
class VetoResult:
    vetoed: bool
    reason: str = ""
    veto_code: str = ""


def check(ticker: str, ticker_data: dict, market_data: dict,
          config: dict, mode: str = "swing") -> VetoResult:
    """
    ticker_data: from engine/ticker_data_adapter.py's ticker_to_dict()
    market_data: from engine/ticker_data_adapter.py's market_to_dict()
    Returns VetoResult immediately on first hit.
    """
    # price used by vetoes 4 (PRICE_RANGE) and 7 (BELOW_AVWAP) below - extracted
    # once here. (Bug fix: this was previously referenced but never assigned,
    # a NameError waiting to happen on veto 4 for every ticker that didn't
    # already get caught by vetoes 1-3 - i.e. almost every call.)
    price = ticker_data.get("price", 0)

    # 1. Earnings Risk Score > 80
    ers = _earnings_risk_score(ticker_data)
    if ers > 80:
        return VetoResult(True, f"Earnings Risk Score {ers}/100", "EARNINGS_RISK")

    # 2. Spread too wide - graded/ATR-aware (see rules/spread_quality.py for
    # the full tiered scale and the rationale for why a flat 0.15%-of-price
    # cliff was replaced). Only the outermost "veto" tier hard-rejects here
    # (0.50% day / 1.00% swing) - the graded tiers below that apply a score
    # penalty instead, from rules/swing_buy_rules.py.
    from rules.spread_quality import evaluate as evaluate_spread
    spread_result = evaluate_spread(ticker_data, mode=mode)
    if spread_result.hard_veto:
        return VetoResult(True, spread_result.reason, "SPREAD_WIDE")

    # 3. Insufficient volume
    avg_vol = ticker_data.get("avg_volume", 0)
    min_vol = 2_000_000 if mode == "day" else 1_000_000
    if avg_vol < min_vol:
        return VetoResult(True, f"Avg volume {avg_vol:,} < {min_vol:,}", "LOW_VOLUME")

    # 4. Price out of range
    if price < 10 or price > 1000:
        return VetoResult(True, f"Price ${price:.2f} outside $10-$1000", "PRICE_RANGE")

    # 5. Negative regulatory news
    # (Breadth collapse was veto #5 in an earlier design. Removed 2026-07-15:
    # single-indicator breadth (McClellan OR A/D) is too noisy to hard-block
    # individual tickers. Breadth now influences scoring via the MARKET_BREADTH
    # bucket and dynamic threshold adjustment, and the market-gate in
    # rules/market_filters.py applies a SCORE PENALTY (not a hard block) for
    # weak breadth, reserving a hard block for a true multi-signal crisis.
    # See rules/market_filters.py's module docstring for full rationale.)
    news_items = ticker_data.get("news_classified", [])
    for item in news_items:
        if item.get("category") == "REGULATORY" and item.get("sentiment", 0.5) < 0.20:
            return VetoResult(True, f"Negative regulatory news: {item.get('headline','')[:60]}", "REG_NEWS")

    # 7. Price below earnings AVWAP
    avwap_earnings = ticker_data.get("avwap_earnings", 0)
    if avwap_earnings and avwap_earnings > 0 and price < avwap_earnings:
        return VetoResult(True, f"Price ${price:.2f} below earnings AVWAP ${avwap_earnings:.2f}", "BELOW_AVWAP")

    # 8. Quote too stale (2026-07-21, external review - this veto used to be
    # permanently dead: quote_age_minutes was hardcoded to 0 because "the
    # quote was fetched this cycle", which conflates "freshly fetched" with
    # "fresh market data" - a provider can return a recently-retrieved but
    # stale underlying quote (pre-market/after-hours/API delays/cached
    # sources/halted symbols). Now genuinely measured from the winning
    # quote provider's own market timestamp when one was supplied this cycle
    # (see ticker_analyzer.py's _parse_quote_time) - gated on
    # quote_age_is_measured so an UNMEASURED quote (no provider timestamp
    # this cycle) can't silently veto as either fresh or stale; it's simply
    # not checked, same as this codebase's behavior before this pass.
    quote_age_min = ticker_data.get("quote_age_minutes", 0)
    quote_age_is_measured = bool(ticker_data.get("quote_age_is_measured", False))
    max_age = 2 if mode == "day" else 30
    if quote_age_is_measured and quote_age_min > max_age:
        return VetoResult(True, f"Quote {quote_age_min:.0f} min old (max {max_age})", "STALE_QUOTE")

    # 9. Kill switch
    if config.get("risk", {}).get("kill_switch_triggered"):
        return VetoResult(True, "Kill switch active", "KILL_SWITCH")

    # 10/11. DAILY_LOSS and PROFIT_LOCK vetoes REMOVED (§54, Phase 2.5).
    #
    # They read risk.daily_loss_limit_triggered and
    # risk.daily_profit_lock_triggered, and no code path in this repository
    # ever set either flag. They could only fire if a human hand-edited
    # config.yaml, while engine/rules_catalog.py advertised both to the
    # operator as live vetoes. That is §9's original finding one layer over: a
    # control that is documented, catalogued and readable, and that cannot
    # fire.
    #
    # No capability is lost. The real daily-loss control is
    # rules/risk_rules.py's RiskEngine.check() plus
    # trip_kill_switch_if_needed(), which use §8's equity-scaled limit rather
    # than a raw dollar cap and route a breach through the kill switch above -
    # a control with a writer, a persist step, a notification and a test. The
    # manual halt is `kill_switch_triggered`, which is the flag actually
    # documented for that purpose.
    #
    # The removal also closes a sharper edge: engine/position_management.py
    # read daily_loss_limit_triggered as a PRIORITY-1 exit-everything trigger,
    # so hand-setting a key nothing writes would liquidate the book. If a
    # deliberate manual flatten is wanted, it should be built as one - with a
    # writer, a test and a UI confirmation - not left as a config key that
    # reads like a limit.

    # 12. Pending cooldown (post-stop-loss)
    from storage.database import Database  # avoid circular imports
    db = Database()
    if db.ticker_in_cooldown(ticker):
        return VetoResult(True, f"{ticker} in cooldown (post-stop)", "COOLDOWN")

    # 13. Day trade specific: time restrictions
    if mode == "day":
        from datetime import datetime
        import pytz
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        hour, minute = now_et.hour, now_et.minute
        # Block dead zone 11:30 AM - 1:30 PM ET
        if (hour == 11 and minute >= 30) or hour == 12 or (hour == 13 and minute < 30):
            return VetoResult(True, "Dead zone 11:30 AM-1:30 PM ET", "DEAD_ZONE")
        # Block after 3:30 PM
        if hour >= 15 and minute >= 30:
            return VetoResult(True, "After 3:30 PM ET — no new day trades", "TOO_LATE")

    # 14. Data completeness
    data_completeness = ticker_data.get("data_completeness_pct", 100)
    if data_completeness < 40:
        return VetoResult(True, f"Data completeness {data_completeness:.0f}% too low", "BAD_DATA")

    # 15. Open position already exists for this ticker
    if db.get_open_position(ticker):
        return VetoResult(True, f"Already have open position in {ticker}", "ALREADY_OPEN")

    # 16. Data Provenance Circuit Breaker - veto #14 above (BAD_DATA) only
    # catches whole-SOURCE dropout (a whole MCP call returning nothing). It's
    # possible for every source to report "complete" while several of the
    # CORE indicators this system's scoring actually depends on (RSI, MACD,
    # TREND, VWAP, market Breadth) individually fell back to a silent
    # default - e.g. yfinance daily bars came back but too thin for a real
    # SMA/EMA, or intraday data was missing so VWAP stayed 0.0. Scoring on
    # a majority-fallback indicator set produces a confident-looking score
    # built on defaults, not real signal - see engine/ticker_analyzer.py's
    # TickerData.stale_indicators and engine/market_breadth.py's
    # is_fallback for where these are actually computed.
    stale = list(ticker_data.get("stale_indicators", []))
    if market_data.get("breadth_stale"):
        stale = stale + ["BREADTH"]
    threshold = config.get("data_quality", {}).get("stale_indicator_veto_threshold", 3)
    if len(stale) >= threshold:
        return VetoResult(
            True,
            f"{len(stale)}/5 core indicators stale/fallback ({', '.join(stale)}) "
            f">= threshold {threshold}",
            "STALE_DATA_CIRCUIT_BREAKER",
        )

    return VetoResult(False)


def _earnings_risk_score(td: dict) -> float:
    """Composite earnings risk: 0-100. Higher = riskier."""
    score = 0.0
    days = td.get("days_to_earnings", 99)
    # == 0, not <= 0 (2026-07-16): negative = finviz reported a PAST
    # earnings date - that's post-earnings, not event risk. Only earnings
    # TODAY scores the maximum.
    if days == 0:
        score += 80
    elif days == 1:
        score += 70
    elif days == 2:
        score += 60
    elif days <= 4:
        score += 40
    elif days <= 7:
        score += 20

    expected_move = td.get("options_expected_move_pct", 0)
    if expected_move > 8:
        score += 20
    elif expected_move > 5:
        score += 10
    elif expected_move > 3:
        score += 5

    hist_move = td.get("historical_earnings_move_avg_pct", 0)
    if hist_move > 12:
        score += 15
    elif hist_move > 7:
        score += 8

    return min(100.0, score)
