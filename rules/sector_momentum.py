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
