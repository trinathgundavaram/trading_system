"""Optional standalone macro-context interpretation prompt.

NOT called in the default cycle - engine/market_context.py already derives the
mechanical flags (vix_is_high, yield_curve_inverted, blackout_active, etc.) from
raw FRED/fear-greed numbers without needing an extra Claude call. This template is
for an on-demand "explain the macro backdrop in plain English" command (e.g. a
dashboard keyboard shortcut), reusing MarketContextData already fetched this cycle.
"""


def build_macro_prompt(mkt) -> str:
    return f"""Interpret the current macro backdrop for a US equity swing/day trader in 2-4 sentences.

Fear & Greed: {mkt.fear_greed_score}/100 ({mkt.fear_greed_rating})
VIX: {mkt.vix_level} {'(elevated)' if mkt.vix_is_elevated else ''} {'(HIGH)' if mkt.vix_is_high else ''}
Yield curve 2s10s: {mkt.yield_curve_spread}bps {'(INVERTED)' if mkt.yield_curve_inverted else ''}
Fed funds rate: {mkt.fed_funds_rate}% | CPI YoY: {mkt.cpi_yoy}% (trend: {mkt.cpi_trend})
Next major macro event: {mkt.next_macro_event} in {mkt.hours_to_next_major_macro}h
Sector leaders: {mkt.sector_leaders} | laggards: {mkt.sector_laggards}

Respond ONLY with this JSON (no markdown, no explanation):
{{"summary": "2-4 sentence plain-English read", "regime": "risk-on|risk-off|neutral|transitional"}}"""
