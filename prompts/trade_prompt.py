"""Builds the per-ticker analysis packet and the combined trade_prompt.md that
you paste into Claude Desktop. This is the hand-off point between the fully
automated, free, MCP-SDK-driven data/rules layer (Python, no Claude involved)
and the one place a human + Claude Desktop + the robinhood-trading MCP are
actually needed: reviewing and placing real orders.
"""
from prompts.analysis_prompt import _f, _pct  # reuse the same None-safe formatters


def build_analysis_packet(ticker, td, mkt, buy_result=None, sell_result=None, position=None) -> str:
    gate_ok, gate_reason = mkt.can_trade_result if hasattr(mkt, "can_trade_result") else (True, "OK")

    buy_section = "N/A (already holding - see sell rules below)"
    if buy_result is not None:
        passed_names = ", ".join(r.name for r in buy_result.rules_passed) or "none"
        failed_names = ", ".join(r.name for r in buy_result.rules_failed) or "none"
        buy_section = (
            f"Buy Score: {buy_result.score:.0f}/{buy_result.max_score:.0f} ({buy_result.pct_score:.0f}%)\n"
            f"Should Buy (rules only): {buy_result.should_buy}\n"
            f"Rules Passed ({len(buy_result.rules_passed)}): {passed_names}\n"
            f"Rules Failed ({len(buy_result.rules_failed)}): {failed_names}\n"
            f"Strongest Signals: {buy_result.strongest_signals}\n"
            f"Weakest Signals: {buy_result.weakest_signals}"
        )

    sell_section = "N/A (no open position)"
    if sell_result is not None:
        names = ", ".join(r.name for r in sell_result.triggered_rules) or "none"
        sell_section = f"Should Sell (rules only): {sell_result.should_sell}\nTriggered Rules: {names}"

    position_section = "NO POSITION - considering entry"
    if position is not None:
        position_section = (
            f"HOLDING {position.shares} shares since {position.entry_time}\n"
            f"Entry: ${_f(position.entry_price)} | Current: ${_f(td.current_price)}\n"
            f"P&L: ${_f(position.unrealized_pnl)} ({_pct(position.pnl_pct)})\n"
            f"Stop Loss: ${_f(position.stop_loss)} | Take Profit: ${_f(position.take_profit)}"
        )

    headlines_str = "; ".join(h.get("title", "") for h in (td.news_headlines or [])[:5]) or "none available"

    return f"""## {ticker}

Price: ${_f(td.current_price)} | Change: {_pct(td.change_pct)} | Volume: {td.volume or '-'} ({_f(td.volume_ratio, "{:.1f}")}x avg)
RSI(14): {_f(td.rsi, "{:.1f}")} | MACD: {td.macd_crossover} | Stoch %K: {_f(td.stochastic_k, "{:.1f}")}
SMA20/50/200: ${_f(td.sma20)} / ${_f(td.sma50)} / ${_f(td.sma200)}   EMA9/21: ${_f(td.ema9)} / ${_f(td.ema21)}
Bollinger %B: {_f(td.bb_pct, "{:.2f}")}   VWAP: ${_f(td.vwap)} ({_pct(td.price_vs_vwap_pct)})
ATR(14): ${_f(td.atr)}   OBV: {td.obv_trend}
Support: {td.support_levels}  Resistance: {td.resistance_levels}

Finviz Technical: {td.finviz_technical_rating}  Analyst: {td.finviz_analyst_rating}  Target: ${_f(td.analyst_target_price)}
Short Float: {_f(td.short_float_pct, "{:.1f}")}%   Insider direction (30d): {td.net_insider_direction}
Sentiment: {_f(td.sentiment_score)}/1.0 ({td.sentiment_label})   Themes: {td.key_themes}
Earnings: {td.earnings_date} ({td.days_to_earnings} days away)
Sector: {td.sector} (rank {td.sector_rank})
Headlines: {headlines_str}

### Rules Engine (deterministic, no LLM)
{buy_section}
{sell_section}

### Position
{position_section}
"""


def generate_trade_prompt(packets: list[str], cfg, mkt=None, gate_reason: str = "OK") -> str:
    header = f"""# Trade Analysis - paste this whole file into Claude Desktop

Auto-trade is **{'ON' if cfg.trading.auto_trade else 'OFF'}** in config.yaml. Kill switch:
**{'ACTIVE' if cfg.risk.kill_switch_triggered else 'off'}**. Market gate: **{gate_reason}**.

You (Claude, reading this in Desktop) have the `robinhood-trading` MCP connected.
For each ticker below:
1. Weigh the rules-engine output plus the technical/fundamental/sentiment data holistically -
   these are inputs, not a verdict. The rules engine never calls an LLM; you are the first
   model to actually reason about this data.
2. Only recommend BUY/SELL if you'd act on it yourself, using `review_equity_order`
   BEFORE `place_equity_order` every time, and respecting:
   - max_position_size_usd: ${cfg.risk.max_position_size_usd}, trade_size_usd: ${cfg.trading.trade_size_usd}
   - max_trades_per_day: {cfg.risk.max_trades_per_day}, max_daily_loss_usd: ${cfg.risk.max_daily_loss_usd}
   - min_confidence: {cfg.risk.min_confidence}% (your own confidence, since there's no automated model score here)
3. If auto_trade is OFF above, treat this as analysis only - describe what you'd do,
   don't place real orders unless the person explicitly asks you to for a specific ticker.
4. If DIRECT EXECUTION is marked ACTIVE below, DO NOT place any orders at all - the
   platform is already executing these signals itself (engine/live_trader.py), and
   placing them again here would DOUBLE every position.

Direct execution: **{'ACTIVE - do not place orders from this prompt' if getattr(cfg.trading, 'live_execution_enabled', False) and cfg.trading.auto_trade and str(getattr(cfg.trading, 'watch_execute', 'WATCH')).upper() == 'EXECUTE' else 'off (execution happens from this prompt, per the rules above)'}**

## MARKET CONTEXT (at time of analysis)
"""
    if mkt is not None:
        header += (
            f"Fear & Greed: {mkt.fear_greed_score}/100 ({mkt.fear_greed_rating})\n"
            f"VIX: {_f(mkt.vix_level, '{:.1f}')} {'⚠ HIGH' if mkt.vix_is_high else ('elevated' if mkt.vix_is_elevated else '✓ OK')}\n"
            f"Yield Curve (2s10s): {_pct(mkt.yield_curve_spread)} {'⚠ INVERTED' if mkt.yield_curve_inverted else '✓ OK'}\n"
            f"Macro Blackout: {mkt.blackout_active} {mkt.blackout_reason or 'None'}\n"
            f"Sector Leaders: {mkt.sector_leaders}  Laggards: {mkt.sector_laggards}\n"
            f"Market Gate: {'BLOCKED - ' + gate_reason if gate_reason != 'OK' else 'OPEN'}\n"
        )
    else:
        header += "(unavailable this cycle)\n"

    body = "\n---\n\n".join(packets)
    return header + "\n---\n\n" + body
