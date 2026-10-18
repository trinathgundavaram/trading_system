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


# ── Theory-driven candidates (canonical published parameters, fixed in advance) ──

def lev_trend(mult):
    """SPY at `mult`x above its 200-day SMA, else intermediate bonds (Faber-style
    timing applied to a levered core)."""
    def f(prices):
        p = prices["SPY"]
        return {"SPY": mult} if p.iloc[-1] > p.iloc[-200:].mean() else {"IEF": 1.0}
    f.rebalance = "B"
    return f


def dual_momentum(mult=1.0):
    """Antonacci Global Equities Momentum: 12-month return; hold the better of
    SPY/EFA if it beats bills (IEF proxy), else bonds."""
    def f(prices):
        r = prices[["SPY", "EFA", "IEF"]].iloc[-1] / prices[["SPY", "EFA", "IEF"]].iloc[-253] - 1
        best = "SPY" if r["SPY"] >= r["EFA"] else "EFA"
        return {best: mult} if r[best] > r["IEF"] else {"IEF": 1.0}
    return f


def vaa(prices):
    """Keller's Vigilant Asset Allocation (aggressive): 13612W momentum on
    SPY/EFA/EEM/IEF(as agg). Any risk asset <=0 -> all to defensive IEF/TLT
    best; else all to the top risk asset."""
    def mom(t):
        p = prices[t]
        return sum(w * (p.iloc[-1] / p.iloc[-1 - 21 * m] - 1) for w, m in ((12, 1), (4, 3), (2, 6), (1, 12)))
    risk = {t: mom(t) for t in ("SPY", "EFA", "EEM", "IEF")}
    if min(risk.values()) <= 0:
        return {max(("IEF", "TLT"), key=mom): 1.0}
    return {max(("SPY", "EFA", "EEM"), key=lambda t: risk[t]): 1.0}


def vol_managed_spy(target=0.12, window=21, max_lev=1.5):
    """Moreira-Muir volatility-managed equity: scale SPY by target/realized vol."""
    def f(prices):
        v = prices["SPY"].pct_change().iloc[-window:].std() * np.sqrt(252)
        k = min(max_lev, target / v) if v > 0 else 1.0
        w = {"SPY": min(k, 1.0 * max_lev)}
        if k < 1.0:
            w["IEF"] = 1.0 - k
        return w
    return f


def risk_parity(target=0.10, window=126, max_lev=2.0):
    """Levered risk parity over SPY/TLT/GLD/DBC (inverse-vol, scaled to target vol)."""
    names = ["SPY", "TLT", "GLD", "DBC"]
    def f(prices):
        r = prices[names].pct_change().iloc[-window:]
        w = (1 / r.std()); w = w / w.sum()
        pv = (r * w).sum(axis=1).std() * np.sqrt(252)
        k = min(max_lev, target / pv) if pv > 0 else 1.0
        return (w * k).to_dict()
    return f


def halloween(prices):
    """Sell-in-May seasonal: SPY Nov-Apr, bonds May-Oct."""
    return {"SPY": 1.0} if prices.index[-1].month in (11, 12, 1, 2, 3, 4) else {"IEF": 1.0}


STRATEGIES = {
    "spy_buy_hold": spy_buy_hold,
    "sixty_forty": sixty_forty,
    "spy_sma200": spy_sma200,
    "trend_riskparity": trend_riskparity,
    "momentum_top3": momentum_top3,
    "trend_rp_voltarget": vol_target(trend_riskparity, target=0.15, max_lev=1.5),
    "rsi2_basket": Rsi2Basket(),
    "core70_rsi2_30": blend([(0.7, spy_buy_hold), (0.3, Rsi2Basket())]),
    "lev_trend_1.5x": lev_trend(1.5),
    "lev_trend_2x": lev_trend(2.0),
    "dual_momentum": dual_momentum(),
    "dual_momentum_1.5x": dual_momentum(1.5),
    "vaa": vaa,
    "vol_managed_spy": vol_managed_spy(),
    "risk_parity_10vol": risk_parity(),
    "halloween": halloween,
    "trendvt70_rsi2_30": blend([(0.7, vol_target(trend_riskparity, target=0.15, max_lev=1.5)), (0.3, Rsi2Basket())]),
}
