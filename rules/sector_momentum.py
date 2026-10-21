"""Sector momentum sleeve rule (2026-10-18). Research basis: research/
strategies2.py::sector_momentum and research/robust_sector.py. Hold the top-N
US sector ETFs by 12-1 month return, equal weight, only while each is above
its own 200-day SMA; the remainder of the sleeve sits in IEF.

Measured 2007-2026 (net 5bps): ~11% CAGR, ~-20% max drawdown vs SPY 12.3% /
-47%. It trails SPY in most bull windows and wins mainly in crashes; treat it
as a drawdown-reducing equity core, not a market beater. The default (n=3,
12m) was the best cell of a 12-cell grid, so expect ~9-10% going forward.
Parameters are fixed here - do not tune them against the same history.
"""
import pandas as pd

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
DEFENSIVE = "IEF"
UNIVERSE = SECTORS + [DEFENSIVE]
MIN_BARS = 254          # 12-1 momentum looks back 252 trading days past the skip month


def target_weights(closes: pd.DataFrame, n: int = 3, leverage: float = 1.0) -> dict:
    """closes: daily closes, columns incl. SECTORS + IEF, oldest first.
    Returns {ticker: weight} summing to `leverage`. Raises ValueError if there
    is too little history, so a data gap can never be read as 'go to cash'."""
    if len(closes) < MIN_BARS or any(t not in closes for t in UNIVERSE):
        raise ValueError(f"need >= {MIN_BARS} bars for {UNIVERSE}; got {len(closes)}")
    mom = (closes[SECTORS].iloc[-22] / closes[SECTORS].iloc[-253] - 1).dropna().sort_values(ascending=False)
    out = {t: leverage / n for t in mom.head(n).index if closes[t].iloc[-1] > closes[t].iloc[-200:].mean()}
    out[DEFENSIVE] = leverage - sum(out.values())
    return {t: w for t, w in out.items() if w > 1e-9}


# ── Blended, levered variant (2026-10-20) ───────────────────────────────────
# research/leverage_study.py "C_blend": 50% sector momentum + 50% broad 3/6/12m
# composite (top 8 of 22 ETFs), both flat to T-bills (SHY) when SPY < its 200d
# SMA, risk positions multiplied by `leverage` (T-bills/bonds stay 1x).
# Measured 2007-2026, 4% financing, 5bps costs: 1.25x -> 11.4% CAGR / -23.4% max
# drawdown (1.0x 10.0% / -18.8%; SPY 12.3% / -47.2%). Bootstrap 10y: median
# 11.1%, 5th pct 4.8%, P(drawdown worse than -35%) ~0.4%. The SPY rule was
# chosen after the 2008 result was seen, so treat the out-of-sample figures as
# optimistic. Fixed parameters - do not tune against the same history.
BROAD = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "LQD", "HYG", "GLD", "SLV", "DBC", "USO",
         "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "IEF"]
CASH = "SHY"
UNIVERSE_BLEND = sorted(set(BROAD) | {CASH})
_DEFENSIVE = {"IEF", CASH}


def _broad_composite(closes: pd.DataFrame, top_n: int = 8) -> dict:
    px = closes[BROAD]
    r = pd.concat([px.iloc[-1] / px.iloc[-1 - 21 * m] - 1 for m in (3, 6, 12)], axis=1).mean(axis=1)
    picks = r.dropna().sort_values(ascending=False).head(top_n)
    return {t: 1.0 / top_n for t in picks.index}


def target_weights_blend(closes: pd.DataFrame, leverage: float = 1.25) -> dict:
    """Weights as fractions of equity (gross can exceed 1 by up to `leverage`)."""
    if len(closes) < MIN_BARS or any(t not in closes for t in UNIVERSE_BLEND):
        raise ValueError(f"need >= {MIN_BARS} bars for {UNIVERSE_BLEND}; got {len(closes)}")
    spy = closes["SPY"]
    if spy.iloc[-1] < spy.iloc[-200:].mean():
        return {CASH: 1.0}
    a, b = target_weights(closes, leverage=1.0), _broad_composite(closes)
    out = {}
    for src in (a, b):
        for t, w in src.items():
            out[t] = out.get(t, 0.0) + w / 2
    return {t: (w if t in _DEFENSIVE else w * leverage) for t, w in out.items() if w > 1e-9}
