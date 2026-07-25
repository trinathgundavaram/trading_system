"""Graded, mode-aware bid/ask spread quality check - replaces the old fixed
0.15%-of-price hard veto that used to live directly in rules/hard_vetoes.py.

RATIONALE (from a deployment review of the old rule): a flat %-of-price
cutoff penalizes low-priced stocks - a 3-cent spread on a $15 stock is 0.20%
and got rejected outright, while a $0.60 spread on an $800 stock is 0.075%
and passed easily, even though the $15 stock's spread is perfectly tradable.
It also ignores that different names/regimes have structurally different
"normal" spreads (mega-caps and ETFs trade tight; small-cap biotech/REITs
trade wider; FOMC/CPI/earnings/the market open widen spreads on otherwise
good stocks temporarily). A single 0.15% cliff rejected all of that
indiscriminately.

This module replaces the single hard cutoff with:
  1. A tiered scale (excellent/good/acceptable/warning/veto) instead of one
     cliff, so a mildly-wide spread costs a few score points instead of an
     outright reject.
  2. A relaxed, mode-aware hard-veto ceiling that only fires on genuinely
     difficult-to-trade spreads - 0.50% for day trading (execution cost
     matters more on a same-day round trip) vs 1.00% for swing trading
     (target move is typically many times the spread, so it can tolerate
     more). Both are far looser than the old flat 0.15% for everything.
  3. spread/ATR surfaced in the reason string as context - a fixed $ spread
     means very different things on a $8-ATR stock vs a $0.40-ATR one.
     Informational only for now (not a separate score input): ATR scales
     with price in roughly the same direction spread-%-of-price already
     does, so folding both into the primary tiers would double-count the
     same underlying "how volatile/liquid is this name" signal.

Hard veto still lives in rules/hard_vetoes.py (SPREAD_WIDE fires only at the
"veto" tier below); the graded, non-veto tiers apply a score penalty from
rules/swing_buy_rules.py instead of rejecting the setup outright.
"""
from dataclasses import dataclass

# Hard-veto ceiling and graded-penalty bands, as % of price (ask-bid)/price.
_BANDS = {
    "day":   {"veto": 0.0050, "warning": 0.0025, "acceptable": 0.0010, "good": 0.0005},
    "swing": {"veto": 0.0100, "warning": 0.0050, "acceptable": 0.0025, "good": 0.0010},
}

# Score points subtracted from the final 0-100 buy score for each tier.
_PENALTY = {"veto": 15.0, "warning": 15.0, "acceptable": 5.0, "good": 0.0, "excellent": 0.0}


@dataclass
class SpreadResult:
    spread_pct: float
    tier: str                  # excellent | good | acceptable | warning | veto
    score_penalty_pct: float   # 0-15, subtracted from the final 0-100 score
    hard_veto: bool
    reason: str


IMPLAUSIBLE_SPREAD_PCT = 0.10  # 10% of price - see guard note in evaluate() below


def evaluate(ticker_data: dict, mode: str = "swing") -> SpreadResult:
    bid = ticker_data.get("bid", 0)
    ask = ticker_data.get("ask", 0)
    price = ticker_data.get("price", 0)
    atr = ticker_data.get("atr", 0)

    if not (price > 0 and bid > 0 and ask >= bid > 0):
        # No usable quote - neutral, same posture as the old code (only
        # evaluated the spread check when bid/ask/price were all present).
        return SpreadResult(0.0, "excellent", 0.0, False, "")

    # Data-quality guard (2026-07-14, in response to Trinath reporting almost
    # nothing was reaching scoring). Root cause confirmed against real
    # production data: 118/167 unscored signals in one day were vetoed here,
    # with "spreads" up to 53% on names like VRSK/IT/DUOL/VOD that do not
    # structurally trade anywhere near that wide. yfinance's free `info`
    # endpoint frequently returns a 0/stale bid or ask outside of a live NBBO
    # feed - engine/ticker_analyzer.py's _parse_yfinance() falls back to
    # `price` independently for EACH side (`td.bid = info.get("bid") or
    # td.price`), so if only ONE side is missing/stale, the other side (a
    # real but possibly stale/wrong quote) can diverge wildly from price and
    # produce a nonsense "spread" that isn't a real market spread at all.
    # Real NBBO spreads essentially never exceed ~10% of price even for
    # illiquid small-caps under normal trading conditions - treat bid/ask
    # that imply more than that as an unreliable quote (neutral, same as the
    # no-quote branch above) rather than a genuine execution-risk veto, so a
    # data glitch can't masquerade as "this stock is untradeable."
    if bid < price * 0.5 or ask > price * 1.5:
        return SpreadResult(0.0, "excellent", 0.0, False,
                             "Bid/ask implausible vs. price - treated as unreliable quote, not a real spread")

    spread_pct = (ask - bid) / price
    if spread_pct > IMPLAUSIBLE_SPREAD_PCT:
        return SpreadResult(spread_pct, "excellent", 0.0, False,
                             f"Spread {spread_pct*100:.2f}% implausibly wide - likely a stale/bad bid or ask, not a real quote (ignored)")
    bands = _BANDS.get(mode, _BANDS["swing"])
    atr_note = f", {(ask - bid) / atr * 100:.0f}% of ATR" if atr else ""

    if spread_pct > bands["veto"]:
        tier = "veto"
    elif spread_pct > bands["warning"]:
        tier = "warning"
    elif spread_pct > bands["acceptable"]:
        tier = "acceptable"
    elif spread_pct > bands["good"]:
        tier = "good"
    else:
        tier = "excellent"

    reason = f"Spread {spread_pct*100:.2f}% ({tier} tier, {mode} mode ceiling {bands['veto']*100:.2f}%){atr_note}"
    return SpreadResult(spread_pct, tier, _PENALTY[tier], tier == "veto", reason)
