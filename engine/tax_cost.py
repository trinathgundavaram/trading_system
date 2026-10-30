"""What a paper sleeve would actually keep: trading costs, financing and an ESTIMATE of short-term tax.

Everything here is a simple model, not tax advice: monthly rebalancing means gains are mostly held < 1 year (taxed as
ordinary income), so one flat `reports.short_term_tax_rate` is applied to net positive gains. Losses are not carried
forward and wash-sale rules are ignored. Use it to compare sleeves after friction, not to file a return."""
from __future__ import annotations

import sqlite3

DEFAULT_RATE = 0.30


def summarize(led, prices: dict, rate: float = DEFAULT_RATE) -> dict | None:
    acct = led.get_sm_account()
    if not acct:
        return None
    with led._conn() as conn:
        conn.row_factory = sqlite3.Row
        closed = [dict(r) for r in conn.execute(f"SELECT realized_pnl FROM {led.p}_positions WHERE status = 'closed'").fetchall()]
    trade_gains = sum(float(r["realized_pnl"] or 0) for r in closed)
    realized_net = float(acct["realized_pnl"] or 0)                    # includes trading costs and financing
    friction = trade_gains - realized_net                              # >0 means costs + interest paid
    unreal = sum((float(prices.get(p["ticker"], p["entry_price"])) - float(p["entry_price"])) * float(p["shares"])
                 for p in led.get_open_sm_positions())
    start = float(acct["starting_cash"])
    tax_realized = max(0.0, trade_gains) * rate
    tax_if_sold = max(0.0, trade_gains + unreal) * rate
    return {"starting_cash": start, "realized_gains": trade_gains, "costs_and_financing": friction,
            "unrealized": unreal, "tax_rate": rate, "tax_on_realized": tax_realized, "tax_if_sold_today": tax_if_sold,
            "after_tax_return_if_sold": (realized_net + unreal - tax_if_sold) / start if start else 0.0,
            "pre_tax_return": (realized_net + unreal) / start if start else 0.0}
