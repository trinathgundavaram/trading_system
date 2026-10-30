"""Cross-check the price feed a sleeve trades on against an independent source.

The stock sleeves read closes from Yahoo only. If Yahoo returns a stale or wrong bar on a rebalance day, the sleeve
trades on it silently. This compares the latest close of SPY plus a few held names against the market-data router
(Alpaca and others, a different provider chain) and reports anything off by more than `tolerance`."""
from __future__ import annotations

TOLERANCE = 0.015      # 1.5%: wider than a normal late-print difference, tight enough to catch a stale/split-broken bar
MAX_NAMES = 6


def mismatches(primary: dict, secondary: dict, tolerance: float = TOLERANCE) -> list[str]:
    """primary/secondary: {ticker: last close}. Tickers missing from either side are ignored."""
    out = []
    for t, a in primary.items():
        b = secondary.get(t)
        if not a or not b:
            continue
        diff = abs(a / b - 1)
        if diff > tolerance:
            out.append(f"{t}: {a:.2f} vs {b:.2f} ({diff:.1%} apart)")
    return out


def secondary_closes(tickers: list[str], router=None) -> dict:
    """Latest close per ticker from the router; any failure for a name just omits it."""
    if router is None:
        from mcp_clients.market_data import MarketDataRouter
        router = MarketDataRouter()
    out = {}
    for t in tickers[:MAX_NAMES]:
        try:
            res = router.get_daily_bars(t)
            if res and res[0]:
                bars = sorted(res[0], key=lambda b: b["date"])
                out[t] = float(bars[-1]["close"])
        except Exception:
            continue
    return out


def check(primary: dict, tickers: list[str], cfg: dict, label: str, router=None) -> list[str]:
    """Run the comparison and notify (once, critical) if anything disagrees. Never raises."""
    try:
        sec = secondary_closes(tickers, router)
        bad = mismatches({t: primary.get(t) for t in tickers[:MAX_NAMES]}, sec)
        if bad:
            from engine.notifications import notify
            notify(cfg, title=f"{label}: price feeds disagree", message="; ".join(bad)[:300],
                   subtitle="Price cross-check", severity="critical")
        return bad
    except Exception:
        return []
