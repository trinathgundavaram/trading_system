"""Main Claude analysis prompt - the Layer 4 'brain' template. Pure string
formatting, no MCP/network calls, so it's the most important thing to unit test
whenever data fields change shape."""


def _f(v, fmt="{:.2f}", default="-"):
    if v is None:
        return default
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def _pct(v, default="-"):
    return _f(v, "{:+.2f}%", default)


def build_analysis_prompt(ticker, td, mkt, buy_result=None, sell_result=None, position=None) -> str:
    rsi_signal = "OVERSOLD" if (td.rsi or 50) < 30 else "OVERBOUGHT" if (td.rsi or 50) > 70 else "neutral"
    stoch_signal = ("OVERSOLD" if (td.stochastic_k or 50) < 20
                     else "OVERBOUGHT" if (td.stochastic_k or 50) > 80 else "neutral")
    macd_crossover = td.macd_crossover or "none"
    bb_position = ("upper" if (td.bb_pct or 0.5) > 0.8 else "lower" if (td.bb_pct or 0.5) < 0.2 else "middle of")
    ema_cross_signal = ("bullish (9>21)" if (td.ema9 or 0) > (td.ema21 or 0) else "bearish (9<21)")
    vix_rating = "ELEVATED" if mkt.vix_is_high else ("elevated" if mkt.vix_is_elevated else "normal")

    upside = None
    if td.analyst_target_price and td.current_price:
        upside = (td.analyst_target_price - td.current_price) / td.current_price * 100

    options_pcr_signal = ""
    if td.options_put_call_ratio is not None:
        options_pcr_signal = "(bearish hedging)" if td.options_put_call_ratio > 1 else "(bullish positioning)"

    short_float_signal = ""
    if td.short_float_pct is not None:
        short_float_signal = "(squeeze risk)" if td.short_float_pct > 15 else "(normal)"

    earnings_warning = ""
    if td.days_to_earnings is not None and td.days_to_earnings < 7:
        earnings_warning = f"  ⚠ Earnings in {td.days_to_earnings} days - event risk"

    headlines_str = "; ".join(h.get("title", "") for h in (td.news_headlines or [])[:5]) or "none available"

    buy_section = "N/A"
    if buy_result is not None:
        passed_names = ", ".join(r.name for r in buy_result.rules_passed) or "none"
        failed_names = ", ".join(r.name for r in buy_result.rules_failed) or "none"
        buy_section = (
            f"Buy Score: {buy_result.score:.0f}/{buy_result.max_score:.0f} ({buy_result.pct_score:.0f}%)\n"
            f"Rules Passed ({len(buy_result.rules_passed)}): {passed_names}\n"
            f"Rules Failed ({len(buy_result.rules_failed)}): {failed_names}\n"
            f"Strongest Signals: {buy_result.strongest_signals}\n"
            f"Weakest Signals: {buy_result.weakest_signals}"
        )

    position_section = "NO POSITION - considering entry"
    if position is not None:
        position_section = (
            f"HOLDING {position.shares} shares since {position.entry_time}\n"
            f"Entry: ${_f(position.entry_price)} | Current: ${_f(td.current_price)}\n"
            f"P&L: ${_f(position.unrealized_pnl)} ({_pct(position.pnl_pct)})\n"
            f"Stop Loss: ${_f(position.stop_loss)} | Take Profit: ${_f(position.take_profit)}\n"
            f"Trailing High: ${_f(position.trailing_high)}"
        )

    sell_warning = ""
    if sell_result is not None and sell_result.should_sell:
        names = ", ".join(r.name for r in sell_result.triggered_rules)
        sell_warning = f"\n⚠ SELL RULES TRIGGERED: {names}"

    return f"""You are an expert quantitative trader with access to institutional-grade data. Analyze {ticker} and make a precise trading decision.

== GLOBAL MARKET CONTEXT ==
Fear & Greed Index: {mkt.fear_greed_score}/100 ({mkt.fear_greed_rating})
  VIX Score: {_f(mkt.vix_level)}  Put/Call Ratio: {_f(mkt.put_call_ratio, "{:.0f}")}  Breadth: {_f(mkt.market_breadth, "{:.0f}")}

VIX Level: {_f(mkt.vix_level)} ({vix_rating})
Yield Curve (2s10s): {_f(mkt.yield_curve_spread, "{:.2f}")}bps {'(INVERTED)' if mkt.yield_curve_inverted else '(normal)'}
Fed Funds Rate: {_f(mkt.fed_funds_rate)}%   CPI (YoY): {_f(mkt.cpi_yoy)}% (trend: {mkt.cpi_trend})
Macro Blackout: {mkt.blackout_active} {mkt.blackout_reason}

Leading Sectors Today: {mkt.sector_leaders}
Lagging Sectors Today: {mkt.sector_laggards}
{ticker} Sector: {td.sector} (rank: {td.sector_rank})

== LIVE QUOTE ==
Price: ${_f(td.current_price)} | Bid: ${_f(td.bid)} | Ask: ${_f(td.ask)}
Change: {_pct(td.change_pct)} | Volume: {td.volume or '-'} ({_f(td.volume_ratio, "{:.1f}")}x avg)
52W Range: ${_f(td.week52_low)} - ${_f(td.week52_high)} (position: {_f(td.pct_52w, "{:.0f}")}%)

== TECHNICAL INDICATORS ==
RSI(14): {_f(td.rsi, "{:.1f}")} {rsi_signal}
Stochastic: K={_f(td.stochastic_k, "{:.1f}")} D={_f(td.stochastic_d, "{:.1f}")} {stoch_signal}
MACD: {_f(td.macd, "{:.3f}")} | Signal: {_f(td.macd_signal, "{:.3f}")} | Hist: {_f(td.macd_hist, "{:.3f}")} {macd_crossover}
Bollinger: Upper=${_f(td.bb_upper)} | Price=${_f(td.current_price)} | Lower=${_f(td.bb_lower)} | %B={_f(td.bb_pct, "{:.2f}")} -> price is {bb_position} the bands
SMA20: ${_f(td.sma20)} {'above' if (td.current_price or 0) > (td.sma20 or 1e9) else 'below'}
SMA50: ${_f(td.sma50)} {'above' if (td.current_price or 0) > (td.sma50 or 1e9) else 'below'}
SMA200: ${_f(td.sma200)} {'above' if (td.current_price or 0) > (td.sma200 or 1e9) else 'below'}
EMA9/21: ${_f(td.ema9)} / ${_f(td.ema21)} {ema_cross_signal}
VWAP: ${_f(td.vwap)} | Price vs VWAP: {_pct(td.price_vs_vwap_pct)}
ATR(14): ${_f(td.atr)} (volatility measure)
OBV Trend: {td.obv_trend}
Support Levels: {td.support_levels}
Resistance Levels: {td.resistance_levels}

== RULES ENGINE RESULT ==
{buy_section}

== FUNDAMENTALS ==
P/E: {_f(td.pe_ratio)} | EPS: ${_f(td.eps)} | Beta: {_f(td.beta)}
Market Cap: ${_f(td.market_cap, "{:,.0f}")}
Finviz Technical Rating: {td.finviz_technical_rating}
Analyst Consensus: {td.finviz_analyst_rating} | Avg Target: ${_f(td.analyst_target_price)} ({_pct(upside)} upside)
Recent Upgrades: {td.recent_upgrades} | Downgrades: {td.recent_downgrades}

== OPTIONS INTELLIGENCE ==
Options Put/Call Ratio: {_f(td.options_put_call_ratio)} {options_pcr_signal}
Implied Volatility: {_f(td.implied_volatility, "{:.1f}")}%
Max Pain: ${_f(td.max_pain_price)}
Unusual Activity: {td.unusual_options_activity}

== INSIDER & OWNERSHIP ==
Short Float: {_f(td.short_float_pct, "{:.1f}")}% {short_float_signal}
Insider Ownership: {_f(td.insider_pct, "{:.1f}")}%
Recent Insider Buys (30d): {td.insider_buys_shares:,} shares
Recent Insider Sells (30d): {td.insider_sells_shares:,} shares
Net Insider Direction: {td.net_insider_direction}
Institutional Ownership: {_f(td.institutional_pct, "{:.1f}")}%

== NEWS SENTIMENT ==
Sentiment Score: {_f(td.sentiment_score)}/1.0 ({td.sentiment_label})
Key Themes: {td.key_themes}
Recent Headlines: {headlines_str}

== EARNINGS ==
Next Earnings: {td.earnings_date} ({td.days_to_earnings} days away){earnings_warning}
Last EPS Surprise: {_pct(td.eps_surprise_pct)}
Revenue Growth (QoQ): {_pct(td.revenue_growth_qoq)}

== CURRENT POSITION ==
{position_section}
{sell_warning}

== YOUR TASK ==
Weigh ALL of the above data holistically. Consider: technical setup quality, sentiment alignment, macro environment, insider signals, options intelligence, and fundamental backdrop.

Respond ONLY with this JSON (no markdown, no explanation):
{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence": 0-100,
  "conviction": "low" | "medium" | "high",
  "reason": "2-3 sentences citing the top 3 factors driving this decision",
  "primary_catalyst": "single most important factor",
  "risk_level": "low" | "medium" | "high",
  "target_price": 0.00,
  "stop_loss": 0.00,
  "hold_hours": 0,
  "position_size_pct": 0-100,
  "key_risks": ["risk1", "risk2"],
  "data_quality": "complete" | "partial" | "limited"
}}"""
