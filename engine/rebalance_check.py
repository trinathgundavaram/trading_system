"""After a rebalance, did the book actually land on the target weights?

Compares each name's actual weight (shares x price / equity) with the target. Differences below `tolerance` are
expected (the minimum-trade floor skips tiny trades); anything bigger means the rebalance logic or the price feed
misbehaved, which would otherwise be invisible because the sleeve just keeps running."""
from __future__ import annotations


def deviations(positions: list, targets: dict, prices: dict, equity: float, tolerance: float = 0.01) -> list[str]:
    if equity <= 0:
        return []
    actual = {p["ticker"]: float(p["shares"]) * float(prices.get(p["ticker"], p.get("entry_price", 0)) or 0) / equity
              for p in positions}
    out = []
    for t in sorted(set(actual) | set(targets)):
        a, w = actual.get(t, 0.0), float(targets.get(t, 0.0))
        if abs(a - w) > tolerance:
            out.append(f"{t}: target {w:.1%}, actual {a:.1%}")
    return out


def check(led, targets: dict, prices: dict, equity: float, cfg: dict, label: str, tolerance: float = 0.01) -> list[str]:
    """Run after a rebalance; notify (critical) when anything is off by more than `tolerance`. Never raises."""
    try:
        bad = deviations(led.get_open_sm_positions(), targets, prices, equity, tolerance)
        if bad:
            from engine.notifications import notify
            notify(cfg, title=f"{label}: rebalance missed its targets", message="; ".join(bad[:6])[:300],
                   subtitle=f"{len(bad)} name(s) off by more than {tolerance:.0%}", severity="critical")
        return bad
    except Exception:
        return []
