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


class Rsi2Basket:
    """Connors RSI(2) pullback on a basket of liquid ETFs, canonical values
    (enter RSI2<10 above SMA200, exit RSI2>70, 10-day time stop). Each ETF owns
    an equal 1/N slot; idle slots stay in cash. Stateful: backtest() calls it
    once per day in order."""
    rebalance = "B"

    def __init__(self, tickers=("SPY", "QQQ", "IWM", "EFA", "EEM", "GLD", "VNQ"), enter=10, exit_=70, max_hold=10):
        self.tickers, self.enter, self.exit_, self.max_hold = list(tickers), enter, exit_, max_hold
        self.held = {}   # ticker -> days held

    @staticmethod
    def _rsi2(p):
        d = p.diff().iloc[-40:]
        up, dn = d.clip(lower=0), (-d).clip(lower=0)
        au, ad = up.ewm(alpha=0.5, adjust=False).mean().iloc[-1], dn.ewm(alpha=0.5, adjust=False).mean().iloc[-1]
        return 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)

    def __call__(self, prices):
        for t in self.tickers:
            p = prices[t].dropna()
            rsi = self._rsi2(p)
            if t in self.held:
                self.held[t] += 1
                if rsi > self.exit_ or self.held[t] >= self.max_hold:
                    del self.held[t]
            elif rsi < self.enter and p.iloc[-1] > p.iloc[-200:].mean():
                self.held[t] = 0
        return {t: 1.0 / len(self.tickers) for t in self.held}


def vol_target(base, target=0.15, window=63, max_lev=1.5):
    """Scale a strategy's weights so the portfolio's trailing vol hits target."""
    def f(prices):
        w = pd.Series(base(prices))
        if w.empty or w.sum() == 0:
            return {}
        r = (prices[w.index].pct_change().iloc[-window:] * w).sum(axis=1)
        vol = r.std() * np.sqrt(252)
        scale = min(max_lev, target / vol) if vol > 0 else 1.0
        return (w * scale).to_dict()
    f.rebalance = getattr(base, "rebalance", None) or "M"
    return f


def blend(parts):
    """parts: [(weight, strategy)] -> one strategy (sums child weight dicts)."""
    def f(prices):
        out = {}
        for wt, s in parts:
            for t, v in (s(prices) or {}).items():
                out[t] = out.get(t, 0.0) + wt * v
        return out
    f.rebalance = "B" if any(getattr(s, "rebalance", "M") == "B" for _, s in parts) else "M"
    return f


STRATEGIES = {
    "spy_buy_hold": spy_buy_hold,
    "sixty_forty": sixty_forty,
    "spy_sma200": spy_sma200,
    "trend_riskparity": trend_riskparity,
    "momentum_top3": momentum_top3,
    "trend_rp_voltarget": vol_target(trend_riskparity, target=0.15, max_lev=1.5),
    "rsi2_basket": Rsi2Basket(),
    "core70_rsi2_30": blend([(0.7, spy_buy_hold), (0.3, Rsi2Basket())]),
    "trendvt70_rsi2_30": blend([(0.7, vol_target(trend_riskparity, target=0.15, max_lev=1.5)), (0.3, Rsi2Basket())]),
}
