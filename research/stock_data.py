"""Point-in-time S&P 500 membership + price loader for the stock-momentum test.

Membership: data/sp500_membership.csv (ticker,start_date,end_date),
from github.com/fja05680/sp500 (MIT) - a ticker is a member from start_date until
end_date (blank = still a member). A ticker can appear in several intervals.

Prices come from yfinance, which has NO data for most companies that were later
acquired, delisted or went bankrupt. coverage() reports how much of the
historical membership therefore has no prices: that missing slice is exactly the
losers, so any result is biased UPWARD by roughly that amount.
"""
from pathlib import Path

import pandas as pd

MEMBERSHIP = Path(__file__).resolve().parent.parent / "data" / "sp500_membership.csv"
CACHE = Path(__file__).resolve().parent / "cache" / "sp500_prices.pkl"


def load_membership() -> pd.DataFrame:
    m = pd.read_csv(MEMBERSHIP, parse_dates=["start_date", "end_date"])
    m["end_date"] = m["end_date"].fillna(pd.Timestamp("2100-01-01"))
    return m


def members_on(m: pd.DataFrame, day) -> set:
    day = pd.Timestamp(day)
    return set(m.loc[(m.start_date <= day) & (m.end_date >= day), "ticker"])


def load_prices(start="2005-06-01", refresh=False) -> pd.DataFrame:
    if CACHE.exists() and not refresh:
        return pd.read_pickle(CACHE)
    import yfinance as yf
    tickers = sorted(load_membership()["ticker"].unique())
    frames = []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = yf.download(chunk, start=start, auto_adjust=True, progress=False, timeout=60, threads=True)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame(chunk[0])
        frames.append(df.dropna(how="all", axis=1))
        print(f"  {min(i + 100, len(tickers))}/{len(tickers)} tickers requested, {sum(f.shape[1] for f in frames)} with data", flush=True)
    px = pd.concat(frames, axis=1).sort_index()
    px = px.loc[:, ~px.columns.duplicated()]
    CACHE.parent.mkdir(exist_ok=True)
    px.to_pickle(CACHE)
    return px


def coverage(m: pd.DataFrame, px: pd.DataFrame, days) -> pd.Series:
    """Share of the true index members on each date that have price history."""
    have = set(px.columns)
    out = {}
    for d in days:
        mem = members_on(m, d)
        out[d] = len(mem & have) / max(len(mem), 1)
    return pd.Series(out)
