"""Cached adjusted-close loader. Prices are cached to disk so a re-run on a
later day reproduces the same numbers (yfinance history is revised over time;
delete the cache file deliberately to refresh)."""
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent / "cache"
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
DEFAULT_START = "2006-06-01"


def load_prices(tickers=None, start=DEFAULT_START, end=None, refresh=False) -> pd.DataFrame:
    tickers = list(tickers or DEFAULT_UNIVERSE)
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"prices_{'_'.join(sorted(tickers))}_{start}_{end or 'latest'}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    import yfinance as yf
    df = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False, timeout=60)["Close"]
    df = df[tickers].dropna(how="all").ffill()
    df.to_csv(path)
    return df
