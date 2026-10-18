"""Candidate strategies. Parameters are the canonical published values and are
FIXED here - do not tune them against the harness's TEST window."""
import numpy as np
import pandas as pd

RISK = ["SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "DBC", "VNQ"]
DEFENSIVE = ["IEF", "TLT"]


def spy_buy_hold(prices):
    return {"SPY": 1.0}


def sixty_forty(prices):
    return {"SPY": 0.6, "IEF": 0.4}


def spy_sma200(prices):
    """Hold SPY while above its 200-day SMA, else intermediate bonds."""
    p = prices["SPY"]
    return {"SPY": 1.0} if p.iloc[-1] > p.iloc[-200:].mean() else {"IEF": 1.0}


def trend_riskparity(prices, lookback=210, vol_window=63):
    """Multi-asset trend: hold each risk asset above its ~10-month SMA, weights
    inverse-volatility; the unfilled slice goes to IEF (not leverage)."""
    held = [t for t in RISK if prices[t].iloc[-1] > prices[t].iloc[-lookback:].mean()]
    if not held:
        return {"IEF": 1.0}
    vol = prices[held].pct_change().iloc[-vol_window:].std()
    iv = 1.0 / vol
    w = (iv / iv.sum()) * (len(held) / len(RISK))
    out = w.to_dict()
    out["IEF"] = 1.0 - w.sum()
    return out


def momentum_top3(prices, n=3):
    """Cross-sectional 12-1 momentum: top-n risk assets, only if their own
    12-1 return is positive; otherwise that slot goes to IEF."""
    mom = (prices[RISK].iloc[-22] / prices[RISK].iloc[-253] - 1).dropna()
    top = mom.sort_values(ascending=False).head(n)
    out = {t: 1.0 / n for t in top.index if top[t] > 0}
    out["IEF"] = 1.0 - sum(out.values())
    return out


STRATEGIES = {
    "spy_buy_hold": spy_buy_hold,
    "sixty_forty": sixty_forty,
    "spy_sma200": spy_sma200,
    "trend_riskparity": trend_riskparity,
    "momentum_top3": momentum_top3,
}
