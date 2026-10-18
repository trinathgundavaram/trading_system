"""Second-generation sleeves on a wider universe. Canonical settings, fixed in
advance (not tuned): 12-month trend/momentum, 63-day vol, 15% vol target."""
import numpy as np
import pandas as pd

from research.strategies import risk_parity, vol_target

UNIVERSE2 = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG",
             "GLD", "SLV", "DBC", "USO",
             "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
TREND_ASSETS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "TLT", "IEF", "LQD", "HYG", "GLD", "SLV", "DBC", "USO"]
SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]


def trend_multi(prices, lookback=252, vol_window=63, risk_per_asset=0.01):
    """Time-series momentum across 14 asset classes (long-only version of the
    'century of evidence' design): hold each asset whose 12-month return is
    positive, sized so each contributes ~equal risk; unused risk budget stays
    in cash. Leverage is capped by the harness."""
    out = {}
    for t in TREND_ASSETS:
        p = prices[t].dropna()
        if len(p) < lookback + 1:
            continue
        if p.iloc[-1] / p.iloc[-lookback] - 1 > 0:
            vol = p.pct_change().iloc[-vol_window:].std() * np.sqrt(252)
            if vol > 0:
                out[t] = (0.15 / len(TREND_ASSETS) * 3) / vol * 0.5   # ~ equal risk, modest gross
    return out


def sector_momentum(prices, n=3):
    """Top-n US sector ETFs by 12-1 month return, equal weight; a slot whose
    own trend (price < SMA200) is down goes to IEF."""
    mom = (prices[SECTORS].iloc[-22] / prices[SECTORS].iloc[-253] - 1).dropna().sort_values(ascending=False)
    out = {}
    for t in mom.head(n).index:
        if prices[t].iloc[-1] > prices[t].iloc[-200:].mean():
            out[t] = 1.0 / n
    out["IEF"] = 1.0 - sum(out.values())
    return out


def combo(parts, target=0.15, max_lev=2.0):
    """Equal-risk blend of sleeves: each sleeve's weights are scaled to its own
    trailing vol, averaged, then the whole portfolio is vol-targeted."""
    def f(prices):
        agg = {}
        for s in parts:
            w = pd.Series(s(prices), dtype=float)
            if w.empty or w.sum() == 0:
                continue
            r = (prices[w.index].pct_change().iloc[-63:] * w).sum(axis=1)
            vol = r.std() * np.sqrt(252)
            k = (0.10 / vol) if vol > 0 else 1.0       # equalize each sleeve to 10% vol
            for t, v in (w * min(k, 2.0)).items():
                agg[t] = agg.get(t, 0.0) + v / len(parts)
        if not agg:
            return {}
        w = pd.Series(agg)
        r = (prices[w.index].pct_change().iloc[-63:] * w).sum(axis=1)
        vol = r.std() * np.sqrt(252)
        k = min(max_lev, target / vol) if vol > 0 else 1.0
        return (w * k).to_dict()
    return f


_rp = risk_parity()
STRATEGIES2 = {
    "spy_buy_hold": lambda p: {"SPY": 1.0},
    "trend_multi": trend_multi,
    "trend_multi_vt15": vol_target(trend_multi, target=0.15, max_lev=2.0),
    "sector_momentum": sector_momentum,
    "risk_parity": _rp,
    "combo_3sleeve_vt15": combo([trend_multi, sector_momentum, _rp], target=0.15),
    "combo_3sleeve_vt10": combo([trend_multi, sector_momentum, _rp], target=0.10),
}
