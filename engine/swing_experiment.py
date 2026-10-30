"""Forward test of the 2026-10-30 swing changes (DAY positions held to the next session's close, wider
stops, greed ceiling). The changes were derived from the same history they were tested on, so the only honest
verdict is out-of-sample: round trips that OPEN on or after EXPERIMENT_START, judged on independent entry days.

Pass criteria, fixed in advance: >= MIN_ENTRY_DAYS distinct entry days AND profit factor > MIN_PF."""
from datetime import datetime

EXPERIMENT_START = "2026-10-30"
MIN_ENTRY_DAYS = 30
MIN_PF = 1.3


def round_trips(trades: list) -> list:
    """FIFO pair buys with later sells per ticker. trades: paper_trades rows (dicts), oldest first."""
    open_, out = {}, []
    for t in trades:
        if t["side"] == "buy":
            open_.setdefault(t["ticker"], []).append(t)
        elif t["side"] == "sell" and open_.get(t["ticker"]):
            b = open_[t["ticker"]].pop(0)
            if not b.get("price"):
                continue
            out.append({"ticker": t["ticker"], "mode": b.get("trade_mode"), "entry": b["created_at"],
                        "exit": t["created_at"], "pnl_pct": (t["price"] / b["price"] - 1) * 100,
                        "pnl": t.get("pnl") or 0.0})
    return out


def summarize(trades: list, start: str = EXPERIMENT_START) -> dict:
    rts = [r for r in round_trips(trades) if str(r["entry"])[:10] >= start]
    days = {str(r["entry"])[:10] for r in rts}
    gains = sum(r["pnl_pct"] for r in rts if r["pnl_pct"] > 0)
    losses = -sum(r["pnl_pct"] for r in rts if r["pnl_pct"] < 0)
    pf = (gains / losses) if losses > 0 else None
    n = len(rts)
    return {"start": start, "round_trips": n, "entry_days": len(days),
            "avg_pct": (sum(r["pnl_pct"] for r in rts) / n) if n else None,
            "win_pct": (sum(1 for r in rts if r["pnl_pct"] > 0) / n * 100) if n else None,
            "profit_factor": pf, "needs_days": MIN_ENTRY_DAYS, "needs_pf": MIN_PF,
            "verdict": ("collecting" if len(days) < MIN_ENTRY_DAYS else
                        "pass" if (pf is not None and pf > MIN_PF) else "fail")}
