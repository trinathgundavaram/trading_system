"""Read-only summary of the paper strategies for the UI's Overview tab.
Pure aggregation over the ledgers - no trading, no network. Each strategy keeps
its own tables; this only reads them side by side."""
import json
from datetime import date, timedelta

import pandas as pd


def _json(db, key, default=None):
    try:
        raw = db.get_sm_state(key)
        return json.loads(raw) if raw else default
    except Exception:
        return default


def next_month_end(today: date) -> str:
    """Last weekday of the current month, or of next month if that has passed."""
    def last_weekday(y, m):
        d = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    d = last_weekday(today.year, today.month)
    if d < today:
        nm = today.month % 12 + 1
        d = last_weekday(today.year + (today.month == 12), nm)
    return d.isoformat()


def _curves(sleeve_rows, swing_rows):
    if not sleeve_rows:
        return {"dates": [], "sleeve": [], "spy": [], "swing": []}
    df = pd.DataFrame(sleeve_rows)
    df.index = pd.to_datetime(df.pop("as_of_date"))
    df = df.sort_index().rename(columns={"total_value": "sleeve", "spy_close": "spy"})
    if swing_rows:
        sw = pd.DataFrame(swing_rows)
        sw.index = pd.to_datetime(sw["timestamp"]).dt.normalize()
        sw = sw["total_value"].groupby(level=0).last().sort_index()
        df["swing"] = sw.reindex(sw.index.union(df.index)).ffill().reindex(df.index)
    out = {"dates": [d.strftime("%Y-%m-%d") for d in df.index]}
    for c in ("sleeve", "spy", "swing"):
        out[c] = [round(float(v), 3) for v in (df[c] / df[c].iloc[0] * 100)] if c in df else []
    return out


def _drawdown(series):
    if len(series) < 2:
        return 0.0
    s = pd.Series(series)
    return float((s / s.cummax() - 1).min())


def build(db, cfg: dict, today: date = None) -> dict:
    today = today or date.today()
    sc = cfg.get("sector_momentum_sleeve", {})
    acct = db.get_sm_account()
    prices = _json(db, "last_prices", {}) or {}
    market = _json(db, "market")
    targets = _json(db, "last_targets")
    positions, mv = [], 0.0
    for p in db.get_open_sm_positions():
        px = prices.get(p["ticker"]) or p["entry_price"]
        value = p["shares"] * px
        mv += value
        positions.append({"ticker": p["ticker"], "shares": p["shares"], "entry_price": p["entry_price"],
                          "price": px, "value": value,
                          "pnl": value - p["shares"] * p["entry_price"],
                          "pnl_pct": (px / p["entry_price"] - 1) if p["entry_price"] else 0.0})
    equity = (acct["cash"] + mv) if acct else None
    for p in positions:
        p["weight"] = (p["value"] / equity) if equity else 0.0
    positions.sort(key=lambda x: -x["value"])
    account = None
    if acct:
        account = {"starting_cash": acct["starting_cash"], "cash": acct["cash"], "equity": equity,
                   "realized_pnl": acct["realized_pnl"],
                   "return_pct": (equity / acct["starting_cash"] - 1) if acct["starting_cash"] else 0.0,
                   "borrowed": max(-acct["cash"], 0.0), "gross": (mv / equity) if equity else 0.0}
    sleeve_rows = db.get_sm_equity_log()
    swing_rows = db.get_paper_equity_history(limit=100000)
    curves = _curves(sleeve_rows, swing_rows)
    stats = {"days": len(curves["dates"])}
    for c in ("sleeve", "spy", "swing"):
        v = curves.get(c) or []
        stats[c + "_total"] = (v[-1] / v[0] - 1) if len(v) > 1 else None
        stats[c + "_dd"] = _drawdown(v) if len(v) > 1 else None
    swing = None
    pa = db.get_paper_account()
    if pa:
        last = swing_rows[-1] if swing_rows else {}
        swing = {"starting_cash": pa["starting_cash"], "cash": pa["cash"], "realized_pnl": pa["realized_pnl"],
                 "total_value": last.get("total_value", pa["cash"]), "open": last.get("n_open", 0),
                 "mode": cfg.get("trading", {}).get("mode"),
                 "scan_minutes": cfg.get("trading", {}).get("day_trade_scan_interval_minutes")}
    rsi2 = None
    if cfg.get("rsi2_sleeve", {}).get("paper_enabled"):
        ra = db.get_rsi2_account()
        rsi2 = {"cash": ra["cash"], "realized_pnl": ra["realized_pnl"]} if ra else {}
    scan = cfg.get("trading", {}).get("day_trade_scan_interval_minutes") or cfg.get("trading", {}).get("scan_interval_minutes")
    jobs = [
        {"name": "Sector momentum", "when": "Mon-Fri 16:15 ET", "enabled": bool(sc.get("paper_enabled")),
         "detail": "daily mark-to-market; rebalances on the last trading day of the month",
         "last": (market or {}).get("as_of")},
        {"name": "Swing engine scan", "when": f"every {scan} min, 9:00-16:30 ET", "enabled": True,
         "detail": "scores the watchlist and screener candidates (paper / WATCH)", "last": None},
        {"name": "RSI(2) SPY sleeve", "when": "Mon-Fri 16:10 ET",
         "enabled": bool(cfg.get("rsi2_sleeve", {}).get("paper_enabled")),
         "detail": "SPY mean-reversion entries and exits", "last": None},
        {"name": "TradingAgents advisor", "when": "Mon-Fri 16:30 ET",
         "enabled": bool(cfg.get("tradingagents_advisor", {}).get("enabled")),
         "detail": "logs LLM ratings only; never trades", "last": None},
    ]
    try:
        from engine import sleeve_drift
        drift = sleeve_drift.evaluate(sleeve_rows, account, cfg, today=today)
    except Exception as e:                          # a monitor must never break the page
        drift = {"status": "watch", "checks": [{"name": "Monitor", "status": "watch", "message": f"drift check failed: {e}"}]}
    return {"drift": drift, "jobs": jobs, "enabled": bool(sc.get("paper_enabled")), "strategy": sc.get("strategy", "sector"),
            "leverage": sc.get("leverage", 1.0), "band_pct": sc.get("band_pct", 0.0),
            "account": account, "positions": positions, "market": market, "targets": targets,
            "curves": curves, "stats": stats, "swing": swing, "rsi2": rsi2,
            "next_rebalance": next_month_end(today)}
