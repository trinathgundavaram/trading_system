"""Layer 4 - builds the structured, ready-to-paste Claude Desktop prompt. This is
the hand-off point between the fully automated, free, MCP-SDK-driven data/rules
layers (Python, zero LLM calls) and the one place a human + Claude Desktop + the
robinhood-trading MCP are actually needed: reviewing and placing real orders."""

# Human-readable rejection reason per bucket tier.
# zero  = 0 pts (nothing fired at all)
# weak  = something fired but not enough to qualify
# close = almost qualified (>= 75% of min_pct)
_BUCKET_REASONS = {
    "TREND": {
        "zero": "No trend established",
        "weak": "Trend weak / mixed",
        "close": "Trend partially established (close to qualifying)",
    },
    "MOMENTUM": {
        "zero": "Momentum absent",
        "weak": "Momentum weak",
        "close": "Momentum building but not yet there",
    },
    "VOLUME_PA": {
        "zero": "No volume/price-action confirmation",
        "weak": "Volume/price action below threshold",
        "close": "Volume/price action close to qualifying",
    },
    "EXTERNAL": {
        "zero": "No positive external catalysts detected (analyst upgrade, insider buying, unusual options)",
        "weak": "External signals thin — only absence-of-bad-news, no positive confirmation",
        "close": "External confirmation partial — one positive signal but not enough",
    },
    "SENTIMENT_MACRO": {
        "zero": "Sentiment/macro unfavorable",
        "weak": "Sentiment/macro headwinds",
        "close": "Sentiment/macro mixed — nearly qualifying",
    },
    "MARKET_BREADTH": {
        "zero": "Market breadth weak — not supporting new buys",
        "weak": "Breadth below threshold",
        "close": "Breadth borderline",
    },
    "VOLATILITY_EXPANSION": {
        "zero": "No volatility compression signal (no squeeze/NR7/inside-day)",
        "weak": "Volatility compression partial",
        "close": "Near a volatility expansion setup",
    },
}


def _safe_num(v, default=0.0) -> float:
    """Display-layer numeric guard (2026-07-15): a live cycle crash
    (`Unknown format code 'f' for object of type 'str'`) proved upstream
    payloads can deliver numerics as strings ('N/A', '12.5'). A display
    line must never kill a cycle - coerce or fall back, never raise."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return default


def _bucket_rejection_reason(b) -> str:
    """Plain-English reason a BucketScore failed, tiered by how far it missed."""
    pct_of_max = (b.points / b.max_points) if b.max_points else 0.0
    min_pct = b.min_pct  # fraction, e.g. 0.50

    reasons = _BUCKET_REASONS.get(b.name, {})
    if pct_of_max == 0:
        tier = "zero"
    elif min_pct > 0 and pct_of_max >= min_pct * 0.75:
        tier = "close"
    else:
        tier = "weak"
    return reasons.get(tier, f"{b.name} did not qualify")


def build_trade_prompt(ticker_packets: list[dict], cfg: dict, position_actions: list = None) -> str:
    """
    Builds the complete ready-to-paste Claude Desktop prompt.
    ticker_packets: list of {ticker, td, buy_result, sell_result, position, market_context}
    position_actions: optional list from engine/position_management.py's Loop B -
        one dict per open position with exit_score/position_health/new_stop/
        priority_action. Rendered as an extra POSITION MANAGEMENT section.
    """
    auto_trade = cfg["trading"]["auto_trade"]
    min_conf = cfg["risk"]["min_confidence"]
    trade_size = cfg["trading"]["trade_size_usd"]

    lines = [
        "You are an expert quantitative trader. I need you to analyze these stocks and take action.",
        "",
        "INSTRUCTIONS:",
        "1. For each ticker below, use your connected MCPs to get additional live data:",
        "   - robinhood-trading MCP: get live quote + my portfolio",
        "   - fear-greed MCP: current Fear & Greed + all 7 sub-indicators",
        "   - finviz MCP: technical rating + analyst consensus",
        "   - stock-scanner MCP: insider trades + earnings calendar",
        "   - fred MCP: Fed funds rate + CPI + yield curve",
        "2. Read the pre-computed analysis packets below",
        "3. Make BUY/SELL/HOLD decision for each ticker",
        f"4. For any signal with confidence >= {min_conf}%:",
    ]

    if auto_trade:
        sizing_enabled = cfg.get("position_sizing", {}).get("enabled", True)
        lines += [
            "   -> Call robinhood-trading MCP review_equity_order first",
            "   -> If no blocking alerts, call place_equity_order",
        ]
        if sizing_enabled:
            lines.append(
                f"   -> Dollar amount: use each ticker's '### SUGGESTED POSITION SIZE' below "
                f"(planned allocation ${trade_size}, scaled per-ticker by score/EV/volatility/portfolio risk) "
                f"- NOT a flat ${trade_size} for every trade"
            )
        else:
            lines.append(f"   -> Dollar amount: ${trade_size} per trade (position sizing disabled in config)")
    else:
        lines += [
            "   -> DO NOT place any trades (auto_trade is OFF)",
            "   -> Show recommendations only",
        ]

    lines += [
        "5. After all tickers, show my updated portfolio from Robinhood MCP",
        "",
        "=" * 60,
        "",
    ]

    for pkt in ticker_packets:
        lines.append(build_ticker_packet(pkt))
        lines.append("")
        lines.append("-" * 60)
        lines.append("")

    if position_actions:
        lines.append("=" * 60)
        lines.append("POSITION MANAGEMENT (open positions - confirm_fill.py-tracked)")
        lines.append("=" * 60)
        lines.append("")
        for a in position_actions:
            lines.append(build_position_action_packet(a))
            lines.append("")
            lines.append("-" * 60)
            lines.append("")

    lines += [
        "=" * 60,
        "SUMMARY TABLE (fill this in after analysis):",
        "| Ticker | Signal | Confidence | Primary Reason | Action |",
        "|--------|--------|------------|----------------|--------|",
    ]
    for pkt in ticker_packets:
        lines.append(f"| {pkt['ticker']} | ? | ?% | ? | ? |")

    return "\n".join(lines)


def build_position_action_packet(action: dict) -> str:
    """Renders one engine/position_management.py Loop B action dict (exit
    score, position health, stop level, priority action) for the trade_prompt.md
    packet. This is advisory only, same as everything else in this file - no
    order is placed from Python."""
    pos = action["position"]
    td = action["ticker_data"]
    exit_result = action["exit_score"]
    health = action["position_health"]
    new_stop = action["new_stop"]
    priority = action["priority_action"]

    lines = [
        f"## POSITION: {action['ticker']}",
        f"Entry ${pos['entry_price']:.2f} | Current ${td.price:.2f} | "
        f"P&L {((td.price - pos['entry_price']) / pos['entry_price'] * 100) if pos['entry_price'] else 0:+.2f}% | "
        f"Days held: {pos.get('days_held', 0):.1f}",
        "",
        f"PRIORITY ACTION: {priority['label']} (priority {priority['priority']}/10"
        + (", URGENT" if priority["urgent"] else "") + ")",
        f"Reason: {priority['reason']}",
        "",
        f"Exit score: {exit_result.total_score:.0f}/100" + (f" — {'; '.join(exit_result.reasons)}" if exit_result.reasons else ""),
        f"Position health: {health.score:.0f}/100 ({health.label})",
        f"Stop: {new_stop.state.value} @ ${new_stop.stop_price:.2f}"
        + (" (advancing this cycle)" if action["stop_should_advance"] else " (unchanged - new level was not better)")
        + f" — {new_stop.stop_reason}",
    ]

    if action["mae_eval"].get("status") not in (None, "insufficient_history"):
        lines.append(f"MAE check: {action['mae_eval']['message']} ({action['mae_eval']['status']})")

    if action["time_stop"]:
        lines.append(f"Time stop: {action['time_stop']['message']}")

    if action["partial_exit"]:
        pe = action["partial_exit"]
        lines.append(f"Partial exit trigger: {pe['label']} ({pe['shares']:.4f} shares)")

    return "\n".join(lines)


def high_vol_line(pr) -> str:
    """§C3: the high-volatility count, WITH the unit it was measured in.

    Pre-§53 this count meant "positions whose STOP DISTANCE was >= threshold";
    post-§53 it means "positions whose ATR AT ENTRY was >= threshold". Same
    label, different quantity - and the old one read systematically low (the
    stop is clamped on volatile names, and it ratchets as a position moves in
    favour, so a winner's measured volatility FELL the better it did). An
    operator comparing today's packet against last week's had no way to know
    the two numbers are not comparable. engine/rules_catalog.py already says
    ATR; this makes the packet agree with the catalogue.

    A module-level function rather than three lines inline so that the wording
    can be tested without constructing a full ticker packet - the label IS the
    deliverable here, and a label nothing asserts on drifts.
    """
    n = pr.high_vol_position_count
    line = f"High-vol positions open (by entry ATR%): {n}"
    proxy_n = getattr(pr, "high_vol_proxy_count", 0)
    if proxy_n:
        # On the line itself rather than in a warning below it. A mixture
        # reported as a plain integer is the failure mode - the number looks
        # measured whichever way it was arrived at.
        line += (f" [{proxy_n} of {n or 'n'} est. from stop distance "
                 f"— pre-migration rows, reads LOW]")
    return line


def build_ticker_packet(pkt: dict) -> str:
    td = pkt["td"]
    br = pkt["buy_result"]
    sr = pkt.get("sell_result")
    pos = pkt.get("position")
    mkt = pkt["market_context"]

    lines = [
        f"## TICKER: {td.ticker}",
        f"Data quality: {td.data_quality}"
        + (f" (missing: {', '.join(td.missing_sources)})" if td.missing_sources else ""),
        "",
        "### MARKET CONTEXT",
        f"Fear & Greed: {mkt.fear_greed_score}/100 ({mkt.fear_greed_rating})",
        f"  VIX sub-score: {mkt.vix_score:.0f} | Put/Call: {mkt.put_call_score:.0f}",
        f"  Breadth: {mkt.breadth_score:.0f} | Momentum: {mkt.momentum_score:.0f}",
        f"  Junk Bond: {mkt.junk_bond_score:.0f} | Safe Haven: {mkt.safe_haven_score:.0f}",
        f"VIX Level: {mkt.vix_level:.1f} {'(ELEVATED)' if mkt.vix_is_elevated else '(OK)'}",
        f"Yield Curve (2s10s): {mkt.yield_spread:+.2f}% {'(INVERTED)' if mkt.yield_curve_inverted else '(OK)'}",
        f"Fed Funds Rate: {mkt.fed_funds_rate:.2f}% | CPI Trend: {mkt.cpi_trend}",
        f"Macro Blackout: {mkt.blackout_reason if mkt.blackout_active else 'None'}",
        f"Sector Leaders: {', '.join(mkt.sector_leaders) or 'N/A'}",
        f"Sector Laggards: {', '.join(mkt.sector_laggards) or 'N/A'}",
        f"{td.ticker} Sector: {td.sector}",
        "",
        "### PRICE",
        f"Price: ${td.price:.2f} | Change: {td.change_pct:+.2f}%",
        f"Volume: {td.volume:,} ({td.volume_ratio:.1f}x avg)",
        f"Bid: ${td.bid:.2f} | Ask: ${td.ask:.2f}",
        f"52W: ${td.w52_low:.2f} - ${td.w52_high:.2f}",
        "",
        "### TECHNICAL INDICATORS",
        f"RSI(14): {td.rsi:.1f}",
        f"Stochastic K/D: {td.stoch_k:.1f} / {td.stoch_d:.1f}",
        f"MACD: {td.macd:.3f} | Signal: {td.macd_signal:.3f} | Hist: {td.macd_hist:.3f}",
        f"MACD Crossover: {td.macd_crossover_direction if td.macd_crossover else 'none'}",
        f"Bollinger %B: {td.bb_pct:.2f} | Upper: ${td.bb_upper:.2f} | Lower: ${td.bb_lower:.2f}",
        f"SMA 20/50/200: ${td.sma_20:.2f} / ${td.sma_50:.2f} / ${td.sma_200:.2f}",
        f"EMA 9/21: ${td.ema_9:.2f} / ${td.ema_21:.2f}",
        f"VWAP: ${td.vwap:.2f}",
        f"ATR(14): ${td.atr:.2f} | OBV: {td.obv_trend}",
        f"Support: {td.support_levels}",
        f"Resistance: {td.resistance_levels}",
        "",
        "### FUNDAMENTALS",
        # _safe_num guards (2026-07-15): the parse layer now coerces too, but
        # a display line must never be able to kill a cycle (see _safe_num).
        f"P/E: {_safe_num(td.pe_ratio):.1f} | EPS: ${_safe_num(td.eps):.2f} | Beta: {_safe_num(td.beta, 1.0):.2f}",
        f"Market Cap: ${_safe_num(td.market_cap) / 1e9:.1f}B",
        f"Technical Rating: {td.technical_rating}",
        f"Analyst Rating: {td.analyst_rating} | Target: ${_safe_num(td.analyst_target):.2f}",
        f"Short Float: {_safe_num(td.short_float):.1f}%",
        f"Insider Activity: {td.insider_net_direction} "
        f"(buys: {td.insider_buys_30d}, sells: {td.insider_sells_30d})",
        f"Options P/C Ratio: {td.options_put_call_ratio:.2f}",
        "",
        "### EARNINGS",
        f"Next Earnings: {td.earnings_date} ({td.days_to_earnings} days away)",
    ]
    if td.days_to_earnings <= 5:
        lines.append("EARNINGS IMMINENT")
    lines += [
        "",
        "### NEWS SENTIMENT",
        f"Score: {td.news_sentiment_score:.2f}/1.0",
        "Headlines:",
    ]
    for h in td.news_headlines[:5]:
        lines.append(f"  - {h}")

    lines += ["", "### RULES ENGINE"]
    score_result = pkt.get("score_result")
    _score_result_for_pd = score_result
    pd = getattr(_score_result_for_pd, "probabilistic_decision", None) if _score_result_for_pd is not None else None

    # ── Score / threshold header ──────────────────────────────────────────────
    # When score is extremely far from the threshold (gap > 25 pts), the
    # threshold detail isn't the interesting part. Lead with the gap, then
    # explain WHY rather than showing formula internals. When the score is
    # close, the threshold breakdown IS informative — show both.
    threshold = None
    threshold_breakdown = None
    if score_result is not None and score_result.threshold_result:
        threshold = score_result.threshold_result.get("final_threshold")
        threshold_breakdown = score_result.threshold_result.get("breakdown")

    score_pct = br.pct_score
    gap = (threshold - score_pct) if (threshold is not None and not br.should_buy) else 0.0
    far_from_threshold = gap > 25.0

    if br.should_buy:
        lines.append(f"Score: {score_pct:.1f}%  |  Threshold: {threshold:.0f}%  →  PASS ✔")
    elif threshold is not None:
        verdict = "FAIL"
        if far_from_threshold:
            lines.append(f"Score: {score_pct:.1f}%  |  Threshold: {threshold:.0f}%  →  {verdict}  ({gap:.0f} pts short)")
        else:
            lines.append(f"Score: {score_pct:.1f}%  |  Threshold: {threshold:.0f}%  →  {verdict}  ({gap:.1f} pts short)")
            if threshold_breakdown:
                lines.append(f"Threshold: {threshold_breakdown}")
    else:
        lines.append(f"Score: {score_pct:.1f}%  |  {'PASS ✔' if br.should_buy else 'FAIL'}")

    # ── Probabilistic decision (when pattern-DB has enough history) ───────────
    if pd is not None and pd.get("mode") == "probabilistic":
        lines += ["", "### PROBABILISTIC DECISION"]
        lines.append(pd.get("headline", ""))
        lines.append(
            f"P(win): {(pd.get('probability_of_success') or 0) * 100:.0f}%  |  "
            f"P(>{pd.get('target_gain_pct', 5):.0f}% gain): {(pd.get('p_target_gain') or 0) * 100:.0f}%  |  "
            f"P(stop-loss range loss): {(pd.get('p_stop_loss') or 0) * 100:.0f}%"
        )
        hold_h = pd.get("expected_hold_hours")
        hold_text = f"{hold_h:.0f}h (~{hold_h / 24:.1f}d)" if hold_h is not None else "n/a"
        lines.append(
            f"Expected return: {pd.get('expected_return_pct', 0):+.1f}%  |  "
            f"Expected drawdown: {pd.get('expected_drawdown_pct', 0):.1f}% (proxy)  |  "
            f"Expected hold: {hold_text}"
        )
        lines.append(
            f"EV: {pd.get('expected_value_pct', 0):+.1f}%  |  "
            f"Based on {pd.get('n_matches')} similar closed trades ({pd.get('confidence')} confidence)"
        )
        if pd.get("threshold_would_have_passed") != pd.get("should_buy"):
            lines.append(
                f"NOTE: score-vs-threshold method would have said "
                f"{'PASS' if pd.get('threshold_would_have_passed') else 'FAIL'} — probabilistic method overrides."
            )

    # ── Bucket table: compact X.X / Y.Y format ───────────────────────────────
    if score_result is not None and getattr(score_result, "buckets", None):
        if score_result.asset_class == "ETF":
            lines.append("(ETF weight profile)")
        lines.append("")
        lines.append("Bucket breakdown:")

        # Sort: qualified first (highest contribution), then unqualified by pct_of_max desc
        def _sort_key(b):
            pct = b.points / b.max_points if b.max_points else 0
            return (0 if b.qualified else 1, -pct)

        sorted_buckets = sorted(score_result.buckets, key=_sort_key)
        max_name_len = max(len(b.name) for b in sorted_buckets)

        for b in sorted_buckets:
            contribution = (b.points / b.max_points) * b.weight * b.qual_mult * 100 if b.max_points else 0.0
            max_contrib = b.weight * 100
            name_padded = b.name.ljust(max_name_len)
            # VOLATILITY_EXPANSION is a bonus-only bucket (min_pct=0): it always
            # "qualifies" in the Boolean sense, so ✔/✘ is meaningless noise.
            # When it contributed 0 pts (no squeeze/NR7/inside-day), show a
            # neutral dash so the reader isn't confused by a ✔ next to 0.0 pts.
            if b.name == "VOLATILITY_EXPANSION" and b.points == 0:
                mark = "–"
            else:
                mark = "✔" if b.qualified else "✘"
            lines.append(
                f"  {mark} {name_padded}   {contribution:4.1f} / {max_contrib:.1f}"
            )

        # ── "Why it failed" section ───────────────────────────────────────────
        # When score is very low, the threshold detail is noise. Replace it
        # with plain-English reasons that are immediately actionable.
        if not br.should_buy:
            lines.append("")
            if far_from_threshold:
                lines.append("Why the score is low:")
            else:
                lines.append("What held it back:")

            unqualified = [b for b in score_result.buckets if not b.qualified and b.min_pct > 0]
            # Sort by how far they missed (worst first: lowest pct_of_max relative to min_pct)
            unqualified.sort(key=lambda b: (b.points / b.max_points) if b.max_points else 0)
            for b in unqualified:
                reason = _bucket_rejection_reason(b)
                contribution = (b.points / b.max_points) * b.weight * b.qual_mult * 100 if b.max_points else 0.0
                max_contrib = b.weight * 100
                lines.append(f"  → {reason}  ({contribution:.1f} / {max_contrib:.1f} pts)")

            # Pattern-DB status (only when far from threshold — near-misses can
            # see the probabilistic-decision section above for this context)
            if far_from_threshold:
                if pd is not None and pd.get("mode") != "probabilistic":
                    n = pd.get("n_matches", 0) or 0
                    lines.append(f"  → Historical evidence: Unavailable (pattern database: {n} similar trades)")

        # Full rule-by-rule checklist for failed buckets — only when score is
        # NOT far from threshold (near-misses need to know exactly which rule
        # to push; blowouts don't — the bucket summary is enough).
        if not far_from_threshold and not br.should_buy:
            for b in score_result.buckets:
                if not b.qualified and b.checklist:
                    lines.append(f"  {b.name} rules:")
                    for rule in b.checklist:
                        mark = "  ✔" if rule["passed"] else "  ✘"
                        lines.append(f"    {mark} {rule['name']}")

    elif br.rules_failed:
        for r in br.rules_failed[:5]:
            lines.append(f"  ✘ {r.name}: {r.detail}")

    if br.top_signals and br.should_buy:
        lines.append("Top signals:")
        for r in br.top_signals:
            lines.append(f"  + {r.name} ({r.weight}pts): {r.detail}")

    eq = getattr(pkt.get("score_result"), "execution_quality", None) if pkt.get("score_result") else None
    if eq is not None:
        lines += [
            "",
            "### EXECUTION QUALITY",
            f"{eq.total_score:.0f}/100 ({eq.tier})",
        ]
        for r in eq.reasons:
            lines.append(f"  - {r}")

    pr = pkt.get("portfolio_risk")
    if pr is not None:
        _hv = high_vol_line(pr)
        lines += [
            "",
            "### PORTFOLIO RISK",
            f"Sector: {pr.sector} ({pr.sector_exposure_pct:.0f}% exposure if added) | "
            f"Themes: {', '.join(pr.themes) or 'none'} ({pr.theme_exposure_pct:.0f}% exposure)",
            f"Portfolio beta if added: {pr.portfolio_beta:.2f} | Max pairwise correlation: {pr.max_pairwise_correlation:.2f} | "
            + _hv,
            f"Size multiplier: x{pr.size_multiplier:.2f}" + (" — BLOCKED" if not pr.allowed else ""),
        ]
        for r in pr.reasons:
            lines.append(f"  ! {r}")
        for w in pr.warnings:
            lines.append(f"  - {w}")

    ps = pkt.get("position_size")
    if ps is not None and ps.applicable:
        lines += [
            "",
            "### SUGGESTED POSITION SIZE",
            f"{ps.suggested_size_pct:.0f}% of planned allocation (${ps.base_allocation_usd:.0f}) "
            f"= ${ps.suggested_dollar_amount:.2f}  [{ps.tier_label} conviction]",
        ]
        for r in ps.reasons:
            lines.append(f"  - {r}")

    if sr and sr.should_sell:
        lines += [
            "",
            "### SELL SIGNAL",
            f"Rule: {sr.triggered_rule}",
            f"Reason: {sr.reason}",
            f"Urgency: {sr.urgency}",
        ]

    if pos:
        pnl = (td.price - pos["entry_price"]) * pos["shares"]
        pnl_pct = ((td.price - pos["entry_price"]) / pos["entry_price"]) * 100 if pos["entry_price"] else 0
        lines += [
            "",
            "### CURRENT POSITION",
            f"Holding {pos['shares']:.4f} shares @ ${pos['entry_price']:.2f}",
            f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)",
            f"Trail high: ${pos.get('trail_high', pos['entry_price']):.2f}",
        ]
    else:
        lines.append("")
        lines.append("### CURRENT POSITION: None")

    return "\n".join(lines)
