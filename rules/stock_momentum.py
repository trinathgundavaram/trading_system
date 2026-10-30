"""Stock momentum sleeve rule (2026-10-20). Research: research/stock_momentum.py.

Among current S&P 500 members with 12 months of history, rank by the average of
the 3/6/12-month returns and hold the top N equal-weight; when SPY is in its
risk-off state (SPY below its 200d SMA with a 2% hysteresis band, same
market_state() as the ETF sleeve) hold T-bills (SHY) instead.

Measured on point-in-time membership 2008-2026 (1.0x, 5bps): ~11.7% CAGR, ~-21%
max drawdown vs SPY ~10.8% / -51%, and it beat all 60 random-pick placebo draws
(median 9.0%). BUT only ~63% of historical members had free price history (the
missing ones are mostly the losers), so absolute returns are optimistic; the
premium over random picks is the more credible number. At 50 bps costs: ~8.3%.
Parameters are fixed here - do not tune them against the same history.
"""
from pathlib import Path

import pandas as pd

from rules.sector_momentum import market_state          # same SPY-vs-200d band rule as the ETF sleeve

CASH = "SHY"
LAGS = (63, 126, 252)                                    # ~3/6/12 trading months
MIN_BARS = 254
DEFAULT_UNIVERSE_FILE = Path(__file__).resolve().parent.parent / "data" / "sp500_membership.csv"


def current_members(path=DEFAULT_UNIVERSE_FILE, today=None) -> list:
    """Tickers that are index members today (start <= today <= end, blank end = open)."""
    m = pd.read_csv(path, parse_dates=["start_date", "end_date"])
    today = pd.Timestamp(today or pd.Timestamp.today().normalize())
    m["end_date"] = m["end_date"].fillna(pd.Timestamp("2100-01-01"))
    return sorted(m.loc[(m.start_date <= today) & (m.end_date >= today), "ticker"].unique())


def momentum_scores(closes: pd.DataFrame, members: list) -> pd.Series:
    if len(closes) < MIN_BARS:
        raise ValueError(f"need >= {MIN_BARS} bars, got {len(closes)}")
    cols = [c for c in members if c in closes.columns]
    last = closes[cols].iloc[-1]
    r = pd.concat([last / closes[cols].iloc[-1 - lag] - 1 for lag in LAGS], axis=1).mean(axis=1, skipna=False)
    return r.dropna().sort_values(ascending=False)


def mom_lowvol_scores(closes: pd.DataFrame, members: list) -> pd.Series:
    """Average of the percentile ranks of 12-1 month momentum and LOW 63-day volatility.
    research/stock_factors.py (with the SPY rule, 1.0x): ~9.8% CAGR, -9.7% max drawdown, Sharpe 0.93 vs the
    composite momentum selection's ~12.1% / -19.6% / 0.81 - about half the drawdown for ~2 points of return,
    which is the better base for leverage. In-sample on a survivorship-biased universe: an option, not a default."""
    if len(closes) < MIN_BARS:
        raise ValueError(f"need >= {MIN_BARS} bars, got {len(closes)}")
    cols = [c for c in members if c in closes.columns]
    mom = (closes[cols].iloc[-22] / closes[cols].iloc[-253] - 1)
    vol = closes[cols].pct_change().iloc[-63:].std()
    df = pd.concat([mom.rank(pct=True), (-vol).rank(pct=True)], axis=1).dropna()
    return df.mean(axis=1).sort_values(ascending=False)


def target_weights(closes: pd.DataFrame, members: list, risk_on: bool, n: int = 40, leverage: float = 1.0,
                   selection: str = "momentum") -> dict:
    """Weights as fractions of equity. Raises if fewer than n stocks are rankable, so a
    data gap can never silently turn into 'hold fewer names' or 'go to cash'.
    selection: "momentum" (3/6/12m composite, the default) or "mom_lowvol"."""
    if not risk_on:
        return {CASH: 1.0}
    scores = mom_lowvol_scores(closes, members) if selection == "mom_lowvol" else momentum_scores(closes, members)
    if len(scores) < n:
        raise ValueError(f"only {len(scores)} rankable stocks (< {n}); check the price download")
    top = scores.head(n)
    return {t: leverage / n for t in top.index}
