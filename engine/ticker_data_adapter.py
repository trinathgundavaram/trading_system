"""Bridges engine/ticker_analyzer.py's TickerData dataclass (and
engine/market_context.py's MarketContextData dataclass) onto the flat dicts
that rules/hard_vetoes.py, rules/swing_buy_rules.py, rules/market_filters.py,
engine/stop_state_machine.py, and engine/position_health.py expect (`.get(key,
default)` throughout, matching the reference implementation given for those
modules).

HONESTY NOTE, same spirit as engine/pattern_features.py: every key below is
tagged REAL (comes from an actual MCP data source already wired up) or
PLACEHOLDER (no data source exists yet - filled with a neutral default so the
new rule modules don't crash, not to fake a signal). Search this file for
"PLACEHOLDER" to see exactly what's still missing before trusting the
6-bucket score or hard vetoes at face value. As real data sources get added
(market breadth, options flow, industry ETFs, earnings AVWAP, etc.), replace
the placeholder line here and nothing downstream needs to change.
"""
from datetime import datetime

from engine import market_breadth


def ticker_to_dict(td, mkt, cfg: dict) -> dict:
    _sector_rs = market_breadth.get_sector_return(td.sector)
    d = {
        # ---- REAL ----
        "price": td.price,
        "bid": td.bid,
        "ask": td.ask,
        "avg_volume": td.avg_volume,
        "days_to_earnings": td.days_to_earnings,
        "sma_20": td.sma_20, "sma_50": td.sma_50, "sma_200": td.sma_200,
        "ema_9": td.ema_9, "ema_21": td.ema_21,
        "rsi": td.rsi, "stoch_k": td.stoch_k,
        "macd": td.macd, "macd_signal": td.macd_signal, "macd_hist": td.macd_hist,
        # macd_positive_days: REAL as of this session - consecutive trailing
        # days MACD histogram has stayed positive (see
        # ticker_analyzer.py's _consecutive_positive_days). Used by
        # rules/swing_buy_rules.py's MOMENTUM bucket to distinguish "just
        # turned positive" from "positive and holding."
        "macd_positive_days": td.macd_positive_days,
        "bb_pct": td.bb_pct, "vwap": td.vwap, "atr": td.atr,
        # change_pct/volume_ratio: REAL, exposed for rules/exit_scorer.py's
        # VOLUME_DISTRIBUTION bucket (a "distribution day" = a down day on
        # above-average volume) - both already real fields on TickerData,
        # just not previously in this dict.
        "change_pct": td.change_pct, "volume_ratio": td.volume_ratio,
        "sector": td.sector,
        "quote_type": td.quote_type,
        "short_float_pct": td.short_float,
        "analyst_consensus": td.analyst_rating,
        "finviz_technical_rating": td.technical_rating,
        # tradingview_rating (2026-07-22): REAL third-party technical gauge
        # from stock-scanner MCP - see rules/swing_buy_rules.py's EXTERNAL
        # bucket for how this and finviz_technical_rating above are
        # reconciled (tradingview preferred when present).
        "tradingview_rating": td.tradingview_rating,
        "obv_rising": td.obv_trend == "rising",
        # obv_trend/macd_crossover_direction/insider_sells_30d: REAL, exposed
        # for rules/exit_scorer.py's VOLUME_DISTRIBUTION/MOMENTUM_WEAKNESS/
        # FUNDAMENTAL_RISK exit buckets - these were already real fields on
        # TickerData (used directly by rules/sell_rules.py) but weren't in
        # this dict yet since nothing reading FROM the dict needed them
        # before the exit engine did.
        "obv_falling": td.obv_trend == "falling",
        "macd_crossover_direction": td.macd_crossover_direction,
        "insider_sells_30d": td.insider_sells_30d,
        "insider_net_buying": td.insider_net_direction == "buying",
        "news_sentiment_score": td.news_sentiment_score,
        "maverick_bullish": td.maverick_sentiment > 0.6,
        "options_put_call_ratio": td.options_put_call_ratio,
        # squeeze/NR7/NR4/inside_day: REAL as of this session - computed from the
        # same daily OHLCV bars as everything else in engine/ticker_analyzer.py's
        # _calc_indicators (see its _calc_volatility_compression method). Used
        # by rules/swing_buy_rules.py's VOLATILITY_EXPANSION bucket.
        "squeeze_active": td.squeeze_active,
        "is_nr7": td.is_nr7,
        "is_nr4": td.is_nr4,
        "is_inside_day": td.is_inside_day,
        # quote_age_minutes (2026-07-21, external review - "freshly fetched
        # does not always mean fresh market data... validate staleness from
        # the provider's market timestamp, not from the time your code
        # performed the request"): now the REAL gap between now and the
        # winning quote provider's own market timestamp when one was
        # supplied this cycle (see ticker_analyzer.py's _parse_quote_time);
        # stays at the old 0.0 "never checked" value - same as this
        # codebase's behavior before this pass - when no provider timestamp
        # was available. quote_age_is_measured tells rules/hard_vetoes.py's
        # STALE_QUOTE veto which case it's in, so an unmeasured quote isn't
        # silently treated as confirmed-fresh.
        "quote_age_minutes": td.quote_age_minutes,
        "quote_age_is_measured": td.quote_age_is_measured,
        # data_completeness derived from the real missing_sources list ticker_analyzer.py tracks
        "data_completeness_pct": _data_completeness_pct(td),
        # stale_indicators: REAL, per-indicator (not just per-source) staleness
        # tracking from ticker_analyzer.py's _calc_indicators - which of
        # RSI/MACD/TREND/VWAP silently fell back to a default this cycle.
        # Feeds rules/hard_vetoes.py's Data Provenance Circuit Breaker veto.
        "stale_indicators": list(td.stale_indicators),
        # news_multiplier: rough linear rescale of the real 0-1 sentiment score onto the
        # 0.7x-2.5x range swing_buy_rules.py expects - an approximation of a real signal,
        # not a fabricated one
        "news_multiplier": 0.7 + (td.news_sentiment_score * 1.8),

        # adx/cmf/donchian_20d_high/recent_swing_low/avwap_swing_low: REAL as
        # of this session - computed from the same daily OHLCV+volume bars as
        # everything else, in engine/ticker_analyzer.py's _calc_indicators /
        # _calc_swing_low_avwap. Used to be PLACEHOLDER(0.0) below.
        "adx": td.adx,
        "plus_di": td.plus_di,
        "minus_di": td.minus_di,
        "cmf": td.cmf,
        "donchian_20d_high": td.donchian_20d_high,
        "recent_swing_low": td.recent_swing_low,
        "avwap_swing_low": td.avwap_swing_low,
        # sector_rs_1d/1m: REAL as of this session - the ticker's own sector
        # ETF's 1d/1m return relative to SPY, computed from the SAME sector-
        # ETF price history engine/market_breadth.py already fetches for the
        # breadth proxy (zero extra MCP calls - see market_breadth.py's
        # get_sector_return()). Used to be PLACEHOLDER(0.0) below.
        "sector_rs_1d": _sector_rs["return_1d"],
        "sector_rs_1m": _sector_rs["return_1m"],
        # Accumulation signals + per-ticker RS vs SPY: REAL as of 2026-07-15
        # (zero-trades audit) - computed in ticker_analyzer.py's
        # _calc_indicators from the same daily OHLCV bars as everything else
        # (obv_new_high_20d / obv_divergence / dollar_vol_ratio_20_50 /
        # accumulation_days_10), and SPY's own cached 1m return from
        # market_breadth.get_spy_return_1m(). Zero extra MCP calls. Used by
        # rules/swing_buy_rules.py's VOLUME_PA accumulation rules and TREND's
        # rs_vs_spy_1m rule.
        "obv_new_high_20d": td.obv_new_high_20d,
        "obv_divergence": td.obv_divergence,
        "dollar_vol_ratio_20_50": td.dollar_vol_ratio_20_50,
        "accumulation_days_10": td.accumulation_days_10,
        "rs_vs_spy_1m": (
            round(td.return_1m_pct - market_breadth.get_spy_return_1m(), 2)
            if td.return_1m_pct else 0.0
        ),
        # Which real market-data provider served quote/bars/news this cycle
        # (mcp_clients/market_data.py); empty dict = yfinance fallback for
        # everything. Rides into data_coverage on every persisted signal.
        "data_sources": dict(td.data_sources),
        # UNKNOWN != FALSE (2026-07-15, external review): True only when at
        # least one EXTERNAL-bucket source actually delivered data this
        # cycle (finviz rating, analyst rating, or any maverick payload).
        # When False, the whole bucket is UNAVAILABLE - not "all its signals
        # are negative" - and rules/swing_buy_rules.py redistributes most of
        # its weight instead of scoring 0/40 (which was punishing every
        # candidate for a data outage; confirmed cause of the 17:00-18:00
        # all-HOLD run where the best stock hit 53.1% vs a 57.1% bar with
        # EXTERNAL at literal zero for all 207 signals).
        "external_data_available": bool(
            (td.technical_rating or "N/A") not in ("", "N/A")
            or (td.tradingview_rating or "N/A") not in ("", "N/A")
            or (td.analyst_rating or "N/A") not in ("", "N/A")
            or td.maverick_data_present
        ),
        # external_unavailable_points / _of_max (2026-07-22, Trinath: "finviz
        # and 2 FMP endpoints cannot be used... fix all the issues"):
        # external_data_available above is all-or-nothing - it only trips
        # when EVERY external source is down, so a candidate missing just
        # maverick+finviz+FMP's estimate/downgrade endpoints (confirmed the
        # common case: finviz's circuit breaker was open on ~1 in 3 cycles,
        # FMP's grades/estimates endpoints have been HTTP-402 dead for a full
        # day) still reads as "available" because ONE weak fallback (e.g.
        # yfinance's recommendationKey) came through, and gets silently
        # docked the other rules' full point value as if that were negative
        # evidence rather than a data gap. This is the PARTIAL counterpart -
        # the actual point-weight of specifically-confirmed-unavailable
        # EXTERNAL rules this cycle (tri-state None fields only, never a
        # measured False) - out of EXTERNAL's own max. rules/swing_buy_rules.py's
        # EXTERNAL bucket uses this to redistribute proportionally instead of
        # the all-or-nothing scale, same 75%-relief/25%-still-costs-something
        # principle as the whole-bucket case, just applied at the rule level
        # instead of only ever firing when 100% of the bucket is dark.
        "external_unavailable_points": sum((
            12 if not td.maverick_data_present else 0,
            10 if (td.technical_rating or "N/A") in ("", "N/A")
                and (td.tradingview_rating or "N/A") in ("", "N/A") else 0,
            6 if td.estimate_raised is None else 0,
            2 if td.recent_downgrade is None else 0,
            6 if td.unusual_options_bullish is None else 0,
        )),
        "external_bucket_max_points": 54,
        # industry_rs_positive: REAL as of this pass - same sector-vs-SPY
        # relative-strength figure as sector_rs_1m above (engine/market_breadth.
        # get_sector_return), reused here for rules/swing_buy_rules.py's
        # EXTERNAL bucket instead of carrying a separate placeholder. This is
        # a sector-level proxy, not true industry-/peer-group-level relative
        # strength (see market_breadth.py's own honesty note on the SPDR
        # sector-ETF proxy) - it's a real, calculated signal, just a coarser
        # one than "industry" implies. Used to be PLACEHOLDER(False) below.
        "industry_rs_positive": _sector_rs["return_1m"] > 0,

        # ---- PLACEHOLDER (no data source wired yet) ----
        # rvol_quality_score: time-normalized as of 2026-07-15 (no-buys-
        # round-2 audit). volume_ratio = today's volume / FULL-DAY average,
        # which structurally reads ~0.3-0.6 for a perfectly normal stock at
        # midday (only half the session's volume has printed yet) - so the
        # rvol tiers were scoring almost every intraday scan as "weak" and
        # VOLUME_PA qualified in just 6.7% of signals. Dividing by the
        # elapsed session fraction compares volume-so-far to the average
        # volume *by this time of day* (flat-pace approximation). Still a
        # proxy, not true minute-binned RVOL, but no longer biased against
        # every scan that runs before 4pm.
        "rvol_quality_score": min(100.0, (td.volume_ratio / _session_elapsed_fraction()) * 40),
        # True weekly resample when >=20/50 trading weeks of daily bars are
        # available (ticker_analyzer, 2026-07-15); falls back to the old
        # daily-proxy approximation otherwise.
        "weekly_above_sma20": (td.weekly_above_sma20 if td.weekly_above_sma20 is not None
                                else td.price > td.sma_20 > 0),
        "weekly_above_sma50": (td.weekly_above_sma50 if td.weekly_above_sma50 is not None
                                else td.price > td.sma_50 > 0),
        # avwap_earnings / no_recent_downgrade / analyst_estimate_raised
        # (2026-07-16, placeholder-fill pass): all three now REAL, sourced
        # from FMP's free-tier /stable endpoints via
        # engine/ticker_analyzer.py - see that file's TickerData field
        # comments and _calc_earnings_avwap() for exactly what's real vs.
        # approximated. td.earnings_date/days_to_earnings (see _parse_finviz)
        # remains a separate, forward-looking NEXT-earnings guess - unrelated
        # to this backward-looking anchor.
        "avwap_earnings": td.avwap_earnings,
        # True unless a real per-bar date index + a confirmed bmo/amc time
        # hint placed the anchor bar exactly (2026-07-21, external review) -
        # see ticker_analyzer.py's _calc_earnings_avwap. Not read by
        # swing_buy_rules.py's scoring itself (avwap_earnings has score AND
        # new-entry-veto authority either way), but surfaced here for
        # data_coverage/confidence logging and future tightening.
        "avwap_earnings_anchor_approximate": td.earnings_avwap_anchor_approximate,
        # Full anchor telemetry (2026-07-21, external review round 2 - "log
        # earnings_avwap_anchor_date, anchor_mode, and anchor_confidence").
        "avwap_earnings_anchor_mode": td.earnings_avwap_anchor_mode,
        "avwap_earnings_anchor_confidence": td.earnings_avwap_anchor_confidence,
        "avwap_earnings_anchor_date": td.earnings_avwap_anchor_date,
        # recent_downgrade is True/False/None; None means the FMP call
        # failed or is unconfigured this cycle - treated as "no credit"
        # (False), never silently upgraded to True, so a data outage can't
        # manufacture bullish evidence the way the old unconditional
        # default-True used to.
        "no_recent_downgrade": td.recent_downgrade is False,
        # recent_downgrade (2026-07-16): the SAME real FMP signal, opposite
        # polarity, for rules/exit_scorer.py's analyst_downgrade - an
        # existing HELD position getting downgraded is bearish evidence for
        # exiting, distinct from no_recent_downgrade's buy-side "is this a
        # clean entry" check. Also None-safe (None -> False -> no credit).
        "recent_downgrade": td.recent_downgrade is True,
        # analyst_estimate_raised: same None-means-no-credit treatment -
        # see storage/database.py's estimate_snapshots table for how the
        # "raised" comparison itself is computed (today's consensus EPS vs.
        # an older stored reading).
        "analyst_estimate_raised": td.estimate_raised is True,
        # Warm-up/measurement metadata (2026-07-21, external review - "do
        # not classify it as simply False. A genuine no-revision observation
        # and insufficient stored history are analytically different"). See
        # storage/database.py's check_and_record_estimate_snapshot() for the
        # full field list (status/score_effect/observed_eps/prior_eps/
        # pct_change/source/snapshot_age_days/analyst_count_change). Not
        # read by scoring - analyst_estimate_raised above remains the single
        # boolean the buy engine acts on - this is for data_coverage/logging
        # so a WARMING_UP ticker is distinguishable from a measured "no
        # raise" in the UI/audit trail.
        "estimate_raised_detail": dict(td.estimate_raised_detail or {}),
        # poc_price / unusual_options_bullish: STILL genuine placeholders.
        # near_poc_support needs multi-week volume-profile data no source in
        # this stack provides (Alpaca's already-fetched intraday bars are
        # 5-day/IEX-only - too shallow for a real POC). unusual_options_bullish
        # was evaluated against github.com/erikmaday/unusual-whales-mcp
        # (2026-07-16) - it IS the real, literal source (actual options-flow
        # alerts, not a volume-skew approximation), but requires a paid
        # UW_API_KEY (no free tier) that isn't configured here. Left as a
        # placeholder rather than faking it with yfinance's coarse call/put
        # skew, per the explicit call not to pass off a partial proxy as the
        # real signal.
        "poc_price": 0.0,
        # unusual_options_bullish (2026-07-22): REAL when stock-scanner MCP's
        # options_unusual_activity tool responded this cycle (tri-state on
        # td.unusual_options_bullish - None means unavailable, never upgraded
        # to a fabricated False/True) - see ticker_analyzer.py's
        # _parse_scanner() and rules/swing_buy_rules.py's EXTERNAL bucket.
        "unusual_options_bullish": td.unusual_options_bullish,
        "options_expected_move_pct": 0.0,
        "historical_earnings_move_avg_pct": 0.0,
        "news_classified": [],  # regulatory-news veto never fires without this
        "rs_percentile": 50.0,
    }
    return d


def market_to_dict(mkt, cfg: dict, spy_td=None) -> dict:
    breadth = market_breadth.calculate(
        spy_price=getattr(spy_td, "price", None),
        spy_sma50=getattr(spy_td, "sma_50", None),
    )
    d = {
        # ---- REAL ----
        "vix": mkt.vix_level,
        "fg_score": mkt.fear_greed_score,
        "yield_spread_2s10s": mkt.yield_spread,
        "upcoming_macro_event": mkt.blackout_reason if mkt.blackout_active else "",

        # ---- REAL, sector-ETF proxy (see engine/market_breadth.py for the
        # honesty note on what "real" means here - genuinely calculated from
        # the 11 SPDR sector ETFs' live price history, not fabricated, but a
        # coarser proxy than true NYSE-wide advance/decline data) ----
        "ad_ratio": breadth["ad_ratio"],
        "mcclellan": breadth["mcclellan"],
        "pct_above_20ema": breadth["pct_above_20ema"],
        "pct_above_50ema": breadth["pct_above_50ema"],
        "breadth_acceleration": breadth["breadth_acceleration"],
        "nh_nl_ratio": breadth["nh_nl_ratio"],
        "ad_slope_5d_positive": breadth["ad_slope_5d_positive"],
        "spy_ad_aligned": breadth["spy_ad_aligned"],
        "opex_status": breadth["opex_status"],  # real calendar calculation, no data source needed
        # Proxy provenance labels (2026-07-15c, external review): every
        # breadth field above is calculated from 11 SPDR sector ETFs, NOT
        # true NYSE/Nasdaq exchange-level advance/decline data. Carried in
        # the market dict so no consumer can conflate proxy breadth with
        # real market internals if a true A/D feed is ever added.
        "breadth_proxy_type": "sector_etf_proxy",
        "breadth_coverage": 11,
        # breadth_stale: True when market_breadth.calculate() had to fall
        # back to its static _NEUTRAL defaults this cycle (sector-ETF fetch
        # failed) rather than a real computed reading - see
        # market_breadth.py's is_fallback. Feeds rules/hard_vetoes.py's Data
        # Provenance Circuit Breaker veto alongside per-ticker
        # stale_indicators above.
        "breadth_stale": bool(breadth.get("is_fallback", False)),
        # ad_ratio_suspect: set by market_breadth.py when ad_ratio is exactly
        # 0.0 or 1.0 (all 11 ETFs moved the same direction — possibly genuine,
        # but also commonly a data artifact from a still-forming bar or stale
        # quote). rules/market_filters.py and rules/dynamic_thresholds.py use
        # this to avoid treating the extreme A/D as a confirmed hard signal.
        "ad_ratio_suspect": bool(breadth.get("ad_ratio_suspect", False)),
        # spy_vs_200dma: ratio of SPY's current price to its 200-day SMA.
        # >1.0 = SPY above 200DMA (uptrend), <1.0 = below (downtrend).
        # Used by rules/market_filters.py's multi-signal crisis gate — one of
        # four conditions that must ALL agree before the scan is hard-blocked.
        # Defaults to 1.0 (neutral/above) when spy_td is unavailable.
        "spy_vs_200dma": (
            (spy_td.price / spy_td.sma_200)
            if spy_td and spy_td.sma_200 and spy_td.sma_200 > 0
            else 1.0
        ),
    }
    return d


def _session_elapsed_fraction() -> float:
    """Fraction of the regular NYSE session (9:30-16:00 ET) elapsed right
    now, clamped to [0.10, 1.0]. Outside market hours returns 1.0 (yesterday's
    full volume vs full average - no normalization needed). The 0.10 floor
    stops the first few minutes after the open from inflating RVOL 20x on
    ordinary opening volume."""
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
        minutes = et.hour * 60 + et.minute
        open_m, close_m = 9 * 60 + 30, 16 * 60
        if minutes <= open_m or minutes >= close_m or et.weekday() >= 5:
            return 1.0
        return max(0.10, (minutes - open_m) / (close_m - open_m))
    except Exception:
        return 1.0


def _data_completeness_pct(td) -> float:
    """Real calculation from the missing_sources list ticker_analyzer.py already
    tracks - not a placeholder, just a simple ratio."""
    missing = len(getattr(td, "missing_sources", []) or [])
    total_sources = 6  # yfinance, maverick TA, maverick sentiment, finviz, scanner, insider
    return max(0.0, (1 - missing / total_sources) * 100)
