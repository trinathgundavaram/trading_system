"""MAE/MFE tracking and learning. Records max adverse excursion and max
favorable excursion per open position, and compares live trades against
historical percentiles (from storage/database.py's mae_mfe_data table) to
flag anomalies once enough history exists."""
import logging

from storage.database import Database

logger = logging.getLogger("trading")


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


def _classify_quality(mae_pct, mfe_pct, outcome_pct, hold_hours) -> str:
    """The §15 sweep, applied at WRITE time.

    Migration 007 quarantines the rows that are already there. This stops the
    same shapes coming back in as 'ok' - because a cleanup that only runs once
    is a cleanup that has to be run again, and the next person to notice will
    be whoever is puzzled by the learning results.

    The two conditions are the ones the audit actually found, and both are
    statements about arithmetic rather than about plausibility:

      A hold time under 36 seconds is not a trade this system can produce -
      the fastest configured cycle is minutes apart (NVDA at +6.67% held for
      12 milliseconds; MU at +10.00% for 10ms).

      A non-zero outcome with zero MAE and zero MFE is impossible, since the
      outcome is itself an excursion. All three of the fabricated rows had
      exactly 0.0 for both.

    Marked, not rejected. Refusing the insert would lose the evidence, and the
    evidence is how the contamination gets traced next time.
    """
    try:
        if hold_hours is not None and float(hold_hours) < 0.01:
            return "synthetic"
        if (float(mae_pct or 0) == 0 and float(mfe_pct or 0) == 0
                and float(outcome_pct or 0) != 0):
            return "synthetic"
    except (TypeError, ValueError):
        return "synthetic"      # unparseable numbers are not a real trade either
    return "ok"


def record_completed(trade: dict) -> None:
    """Called when a trade closes (confirm_fill.py's and paper_trader.py's
    sell paths). Stores MAE/MFE for future learning.

    §15: the caller is expected to have overwritten pnl_pct/pnl/hold_hours
    with close_position()'s figures before calling. Do not recompute them from
    the trade row here - that second computation is exactly what recorded ADPT
    as -1.88% over 6.34h in paper_trades and -3.20% over 5.0h in this table.
    """
    db = Database()
    mae = trade.get("max_adverse_excursion_pct") or 0
    mfe = trade.get("max_favorable_excursion_pct") or 0
    outcome = trade.get("pnl_pct", 0)
    hold = trade.get("hold_hours", 0)
    quality = _classify_quality(mae, mfe, outcome, hold)
    if quality != "ok":
        logger.warning(
            f"mae_mfe: {trade.get('ticker')} recorded as data_quality="
            f"{quality!r} (outcome {outcome}%, hold {hold}h, MAE {mae}, "
            f"MFE {mfe}) - it will NOT train anything. If this came from a "
            f"real trade, the bug is upstream in how those figures were set.")
    db.insert_mae_mfe({
        "trade_id": trade.get("id"),
        "ticker": trade["ticker"],
        "setup_type": trade.get("setup_type") or "unknown",
        "regime": trade.get("entry_regime") or "UNKNOWN",
        "mae_pct": mae,
        "mfe_pct": mfe,
        "outcome_pct": outcome,
        "hold_hours": hold,
        "data_quality": quality,
    })
