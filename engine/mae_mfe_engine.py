"""MAE/MFE tracking and learning. Records max adverse excursion and max
favorable excursion per open position, and compares live trades against
historical percentiles (from storage/database.py's mae_mfe_data table) to
flag anomalies once enough history exists."""
from storage.database import Database


def update_live(position_id, ticker_data: dict, entry_price: float) -> dict:
    """Called every cycle for open positions. Updates MAE/MFE on the position row."""
    current = ticker_data.get("price", entry_price)
    db = Database()
    pos = db.get_position(position_id)
    if not pos:
        return {}

    updates = {}

    # MAE: how far did price go against us from entry?
    drawdown = (entry_price - current) / entry_price * 100 if entry_price else 0.0
    if drawdown > (pos.get("max_adverse_excursion_pct") or 0):
        updates["max_adverse_excursion_pct"] = drawdown

    # MFE: how far did price go in our favor?
    gain = (current - entry_price) / entry_price * 100 if entry_price else 0.0
    if gain > (pos.get("max_favorable_excursion_pct") or 0):
        updates["max_favorable_excursion_pct"] = gain
        updates["high_watermark_price"] = current

    if updates:
        db.update_position(position_id, updates)

    return updates


def evaluate_mae_percentile(position: dict, setup_type: str, regime: str) -> dict:
    """Compare current MAE to the historical percentile for winning trades of
    the same setup_type/regime. Needs >= 10 historical winners before it says
    anything - with a fresh mae_mfe_data table (this is a brand-new feature),
    expect "insufficient_history" for a while."""
    current_mae = position.get("max_adverse_excursion_pct") or 0
    db = Database()

    historical = db.query_mae_winners(setup_type, regime)
    if len(historical) < 10:
        return {"status": "insufficient_history", "n": len(historical), "current_mae": current_mae}

    sorted_maes = sorted(historical)
    n = len(sorted_maes)
    percentile = sum(1 for m in sorted_maes if m < current_mae) / n * 100
    p75 = sorted_maes[int(n * 0.75)]
    p90 = sorted_maes[min(n - 1, int(n * 0.90))]

    if percentile > 90:
        status = "anomalous"
        rec = "Increase exit score — behaving differently from winners"
    elif percentile > 75:
        status = "elevated"
        rec = "Tighten stop — elevated drawdown vs historical winners"
    else:
        status = "normal"
        rec = "Within normal range"

    return {
        "current_mae": current_mae, "percentile": percentile,
        "p75": p75, "p90": p90, "status": status, "recommendation": rec,
        "n_historical": n,
        "message": f"Current drawdown {current_mae:.1f}% = {percentile:.0f}th percentile of historical winning trades",
    }


def record_completed(trade: dict) -> None:
    """Called when a trade closes (confirm_fill.py's sell path). Stores
    MAE/MFE for future learning."""
    db = Database()
    db.insert_mae_mfe({
        "trade_id": trade.get("id"),
        "ticker": trade["ticker"],
        "setup_type": trade.get("setup_type") or "unknown",
        "regime": trade.get("entry_regime") or "UNKNOWN",
        "mae_pct": trade.get("max_adverse_excursion_pct") or 0,
        "mfe_pct": trade.get("max_favorable_excursion_pct") or 0,
        "outcome_pct": trade.get("pnl_pct", 0),
        "hold_hours": trade.get("hold_hours", 0),
    })
