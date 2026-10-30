"""A free "second opinion" on every swing BUY: a devil's-advocate checklist run on data the engine already has.

Borrowed from the multi-agent trading-framework idea (independent analysts argue a bull and a bear case before a
risk manager decides) without any LLM, so it costs nothing and is deterministic. The 7-bucket score is the bull case;
this lists the red flags it can miss (chasing an extended move, event risk, weak participation, a long-term
downtrend...). It is ADVISORY: it never blocks a trade. Each opinion is logged, and `outcome_stats` later compares
real paper results by verdict, so the checklist only earns a veto if the data shows its flags predict losers."""
from __future__ import annotations

from datetime import datetime, timedelta

AGREE, CAUTION, DISAGREE = "AGREE", "CAUTION", "DISAGREE"


def _f(d: dict, k: str):
    v = d.get(k)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def evaluate(t: dict) -> dict:
    """t: engine/ticker_data_adapter.ticker_to_dict output. Missing fields simply do not vote."""
    price = _f(t, "price")
    bear, bull = [], []

    def flag(cond, text, into):
        if cond:
            into.append(text)

    sma20, sma50, sma200 = _f(t, "sma_20"), _f(t, "sma_50"), _f(t, "sma_200")
    rsi, bb, chg = _f(t, "rsi"), _f(t, "bb_pct"), _f(t, "change_pct")
    vr, atr, dte = _f(t, "volume_ratio"), _f(t, "atr"), _f(t, "days_to_earnings")
    hist, pcr, news = _f(t, "macd_hist"), _f(t, "options_put_call_ratio"), _f(t, "news_sentiment_score")
    bid, ask, ins = _f(t, "bid"), _f(t, "ask"), _f(t, "insider_sells_30d")

    flag(dte is not None and 0 <= dte <= 5, "earnings within 5 days", bear)
    flag(rsi is not None and rsi > 72, "RSI above 72 - overbought", bear)
    flag(price and sma20 and price > sma20 * 1.08, "more than 8% above its 20-day average (extended)", bear)
    flag(bb is not None and bb > 1.0, "above the upper Bollinger band", bear)
    flag(chg is not None and chg > 6, "already up more than 6% today (chasing)", bear)
    flag(price and sma200 and price < sma200, "below the 200-day average (long-term downtrend)", bear)
    flag(price and sma50 and price < sma50, "below the 50-day average", bear)
    flag(vr is not None and vr < 0.8, "volume below average (weak participation)", bear)
    flag(price and atr and atr / price > 0.06, "very volatile (ATR above 6% of price)", bear)
    flag(hist is not None and hist < 0, "MACD histogram negative", bear)
    flag(ins is not None and ins >= 3, "heavy insider selling (30d)", bear)
    flag(news is not None and news < -0.3, "negative news sentiment", bear)
    flag(pcr is not None and pcr > 1.3, "options skewed to puts", bear)
    flag(price and bid and ask and (ask - bid) / price > 0.005, "wide bid-ask spread", bear)
    if isinstance(t.get("analyst_consensus"), str):
        flag("sell" in t["analyst_consensus"].lower(), "analyst consensus is Sell", bear)

    flag(price and sma50 and sma200 and price > sma50 > sma200, "price above rising 50/200-day averages", bull)
    flag(hist is not None and hist > 0, "MACD histogram positive", bull)
    flag(vr is not None and vr > 1.3, "volume well above average", bull)
    flag(bool(t.get("obv_rising")), "on-balance volume rising", bull)
    flag(bool(t.get("insider_net_buying")), "net insider buying", bull)
    flag(rsi is not None and 45 <= rsi <= 65, "RSI in a healthy range", bull)
    flag(bool(t.get("squeeze_active")), "volatility squeeze setting up", bull)
    flag(news is not None and news > 0.3, "positive news sentiment", bull)

    bear, bull = [b for b in bear if b], [b for b in bull if b]
    n = len(bear)
    verdict = AGREE if n <= 1 else CAUTION if n <= 3 else DISAGREE
    return {"verdict": verdict, "bear": bear, "bull": bull}


def outcome_stats(opinions: list, round_trips: list, window_minutes: int = 30) -> dict:
    """Match each closed round trip to the opinion logged just before its entry; return per-verdict n / avg % / win %.
    opinions: [{ticker, created_at, verdict}], round_trips: engine.swing_experiment.round_trips output."""
    by_t: dict = {}
    for o in opinions:
        by_t.setdefault(o["ticker"], []).append(o)
    groups: dict = {}
    for rt in round_trips:
        try:
            entry = datetime.fromisoformat(str(rt["entry"]))
        except ValueError:
            continue
        best = None
        for o in by_t.get(rt["ticker"], []):
            ts = datetime.fromisoformat(str(o["created_at"]))
            if timedelta(0) <= entry - ts <= timedelta(minutes=window_minutes):
                best = o if best is None or ts > datetime.fromisoformat(str(best["created_at"])) else best
        if best:
            groups.setdefault(best["verdict"], []).append(rt["pnl_pct"])
    return {v: {"n": len(x), "avg_pct": sum(x) / len(x), "win_pct": 100 * sum(1 for p in x if p > 0) / len(x)}
            for v, x in groups.items()}
