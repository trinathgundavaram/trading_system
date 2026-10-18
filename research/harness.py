"""Fixed benchmark harness. A strategy is a function
    f(prices: DataFrame up to and including day t) -> {ticker: weight}
called at each rebalance date. Weights are decided on the close of day t and
EARN returns from day t+1 (no lookahead). Costs are charged per unit of
turnover. Results are reported net of costs, versus SPY, on a frozen
TRAIN/TEST split and on named stress windows.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252
SPLIT_DATE = "2016-01-01"          # TRAIN < split <= TEST; fixed, never tuned
STRESS_WINDOWS = {"2008": ("2008-01-01", "2009-03-31"), "2022": ("2022-01-01", "2022-12-31")}


@dataclass
class Config:
    cost_bps: float = 5.0           # per unit of one-way turnover
    rebalance: str = "M"            # pandas offset alias; "M" = month-end
    dd_cap: float = 0.35            # max acceptable drawdown (fraction)
    max_leverage: float = 1.0       # gross exposure cap; >1 borrows at financing_bps
    financing_bps: float = 400.0    # annual cost on the borrowed part (gross - 1)
    warmup_days: int = 260          # history a strategy gets before first trade


def backtest(strategy, prices: pd.DataFrame, cfg: Config = Config()) -> pd.Series:
    """Net daily return series."""
    rets = prices.pct_change().fillna(0.0)
    idx = prices.index
    freq = getattr(strategy, "rebalance", cfg.rebalance)   # a strategy may rebalance daily ("B")
    reb = set(prices.groupby(prices.index.to_period(freq)).tail(1).index)
    w = pd.Series(0.0, index=prices.columns)
    out = np.zeros(len(idx))
    for i, d in enumerate(idx):
        gross = float((w * rets.loc[d]).sum())
        # drift weights with the day's returns
        if w.sum() > 0:
            w = w * (1 + rets.loc[d]) / (1 + gross)
        cost = 0.0
        if d in reb and i >= cfg.warmup_days:
            raw = strategy(prices.iloc[: i + 1]) or {}
            tgt = pd.Series(raw, dtype=float).reindex(prices.columns).fillna(0.0)
            tgt = tgt.clip(lower=0.0)
            if tgt.sum() > cfg.max_leverage + 1e-9:
                tgt = tgt / tgt.sum() * cfg.max_leverage   # long-only; leverage only up to max_leverage
            cost = float((tgt - w).abs().sum()) * cfg.cost_bps / 1e4
            w = tgt
        borrowed = max(float(w.sum()) - 1.0, 0.0)
        out[i] = gross - cost - borrowed * cfg.financing_bps / 1e4 / TRADING_DAYS
    return pd.Series(out, index=idx)


def metrics(r: pd.Series, bench: pd.Series | None = None) -> dict:
    r = r.dropna()
    if len(r) < 2:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / TRADING_DAYS
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(TRADING_DAYS)
    dd = (eq / eq.cummax() - 1).min()
    m = {"cagr": cagr, "vol": vol, "sharpe": (r.mean() * TRADING_DAYS) / vol if vol else 0.0,
         "maxdd": dd, "calmar": cagr / abs(dd) if dd else 0.0, "total": eq.iloc[-1] - 1}
    if bench is not None:
        b = bench.reindex(r.index).fillna(0.0)
        cov = np.cov(r, b)
        m["beta"] = cov[0, 1] / cov[1, 1] if cov[1, 1] else 0.0
        m["excess_cagr"] = cagr - metrics(b)["cagr"]
    return m


def evaluate(strategy, prices, cfg: Config = Config()) -> dict:
    r = backtest(strategy, prices, cfg)
    spy = prices["SPY"].pct_change().fillna(0.0)
    start = prices.index[cfg.warmup_days]
    r, spy = r[start:], spy[start:]
    res = {"train": metrics(r[:SPLIT_DATE].iloc[:-1], spy), "test": metrics(r[SPLIT_DATE:], spy),
           "full": metrics(r, spy), "spy_test": metrics(spy[SPLIT_DATE:]), "spy_full": metrics(spy)}
    for name, (a, b) in STRESS_WINDOWS.items():
        ok = len(r[a:b]) > 20
        res[name] = metrics(r[a:b]) if ok else {}
        res["spy_" + name] = metrics(spy[a:b]) if ok else {}
    res["accept"] = acceptance(res, cfg)
    return res


def acceptance(res: dict, cfg: Config = Config()) -> dict:
    """The single pass/fail test: out-of-sample (TEST), net of costs, vs SPY,
    plus the stress windows. All checks must hold."""
    t, s = res["test"], res["spy_test"]
    checks = {
        "test_sharpe_beats_spy": t["sharpe"] > s["sharpe"],
        "test_return_ok": t["cagr"] >= s["cagr"],
        "test_dd_within_cap": abs(t["maxdd"]) <= cfg.dd_cap,
    }
    for k in STRESS_WINDOWS:
        if res.get(k):
            checks[f"{k}_drawdown_le_spy"] = res[k]["maxdd"] >= res["spy_" + k]["maxdd"]
    checks["passed"] = all(checks.values())
    return checks
