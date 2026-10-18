"""Sector-momentum sleeve - PAPER trading on its own ledger (sm_account /
sm_positions). Never touches a broker and never touches paper_trader.py's or
the RSI2 sleeve's tables. The rule is rules/sector_momentum.py; this module
only turns target weights into simulated fills.

Fills are at the supplied close with `cost_bps` of slippage each way, matching
the research harness (decide at the close, trade at the close)."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading")
MIN_TRADE_FRAC = 0.01     # ignore rebalance deltas below 1% of equity


def ensure_seeded(db, cfg: dict) -> dict:
    account = db.get_sm_account()
    if account:
        return account
    cash = float(cfg.get("sector_momentum_sleeve", {}).get("paper_starting_cash", 10000.0))
    logger.info(f"Sector-momentum sleeve paper account seeded: ${cash:.2f}")
    return db.init_sm_account(cash)


def equity(db, prices: dict) -> float:
    acct = db.get_sm_account()
    return acct["cash"] + sum(p["shares"] * prices[p["ticker"]] for p in db.get_open_sm_positions()
                              if p["ticker"] in prices)


def rebalance(db, cfg: dict, targets: dict, prices: dict, date: str = None, cost_bps: float = 5.0) -> list:
    """Move the ledger to `targets` ({ticker: weight of equity}). Sells first so
    the cash exists for the buys. Returns a list of trade dicts."""
    ensure_seeded(db, cfg)
    date = date or datetime.now(timezone.utc).date().isoformat()
    open_pos = {p["ticker"]: p for p in db.get_open_sm_positions()}
    missing = [t for t in set(targets) | set(open_pos) if not prices.get(t)]
    if missing:
        raise ValueError(f"no price for {missing} - refusing to rebalance on partial data")
    eq = equity(db, prices)
    slip = cost_bps / 1e4
    trades = []
    for t, pos in sorted(open_pos.items()):          # sells / trims
        want = targets.get(t, 0.0) * eq / prices[t]
        sell = pos["shares"] - want
        if sell * prices[t] < MIN_TRADE_FRAC * eq and t in targets:
            continue
        sell = min(sell, pos["shares"])
        px = prices[t] * (1 - slip)
        pnl = sell * (px - pos["entry_price"])
        if sell >= pos["shares"] - 1e-9:
            db.close_sm_position(pos["id"], date, px, pnl)
        else:
            db.update_sm_position_shares(pos["id"], pos["shares"] - sell)
        db.adjust_sm_cash(sell * px, realized_pnl_delta=pnl)
        trades.append({"ticker": t, "side": "sell", "shares": sell, "price": px, "pnl": pnl})
    open_pos = {p["ticker"]: p for p in db.get_open_sm_positions()}
    for t, w in sorted(targets.items()):             # buys / adds
        cur = open_pos[t]["shares"] if t in open_pos else 0.0
        buy = w * eq / prices[t] - cur
        px = prices[t] * (1 + slip)
        cash = db.get_sm_account()["cash"]
        buy = min(buy, cash / px)
        if buy * px < MIN_TRADE_FRAC * eq:
            continue
        if t in open_pos:
            p = open_pos[t]
            avg = (p["entry_price"] * p["shares"] + px * buy) / (p["shares"] + buy)
            db.update_sm_position_shares(p["id"], p["shares"] + buy)
            with db._conn() as conn:
                conn.execute("UPDATE sm_positions SET entry_price = ? WHERE id = ?", (avg, p["id"]))
        else:
            db.open_sm_position(t, date, px, buy)
        db.adjust_sm_cash(-buy * px)
        trades.append({"ticker": t, "side": "buy", "shares": buy, "price": px})
    logger.info(f"Sector-momentum sleeve rebalance {date}: {[(x['side'], x['ticker']) for x in trades]}")
    return trades


def snapshot(db, prices: dict) -> dict:
    acct = db.get_sm_account()
    if not acct:
        return {"seeded": False}
    eq = equity(db, prices)
    return {"seeded": True, "cash": acct["cash"], "starting_cash": acct["starting_cash"],
            "realized_pnl": acct["realized_pnl"], "total_value": eq,
            "positions": {p["ticker"]: p["shares"] for p in db.get_open_sm_positions()},
            "total_return_pct": round((eq / acct["starting_cash"] - 1) * 100, 2)}
