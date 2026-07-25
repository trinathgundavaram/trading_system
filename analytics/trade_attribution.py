"""Trade Attribution - Priority 4 from the deployment review: "You're already
collecting MAE/MFE and learning data. The next step is understanding why
trades won or lost." For every closed trade, classify the PRIMARY reason
into one of:
    TREND_FAILURE, MARKET_DETERIORATION, NEWS_EVENT, STOP_TOO_TIGHT,
    ENTRY_TOO_EARLY, PROFIT_TARGET_TOO_CONSERVATIVE, EXIT_SCORE_DETERIORATION

Operates on the SAME data shape analytics/performance.py already established
as this codebase's "closed trades" source (db.get_patterns(closed_only=True)
- a list of dicts with `features`/`outcome_pct`/`hold_hours` - see that
module's docstring: "works before a dedicated closed trades reporting table
exists"), enriched with storage/database.py's mae_mfe_data table (MAE/MFE is
only recorded for REAL closed trades via confirm_fill.py's sell path -
engine/mae_mfe_engine.py's record_completed() - so simulated/time-based
pattern closes classify with a coarser, MAE/MFE-less rule set and a lower
confidence label).

HONESTY NOTE, same convention as the rest of this codebase: this is a
RULE-BASED classifier over real recorded numbers (MAE, MFE, outcome, hold
time, entry regime/bucket scores), not a machine-learned or LLM
classification - there's no labeled "ground truth reason" dataset to train
on. Each classification includes the evidence it fired on, so a human can
sanity-check the call rather than trust a black box.
"""
from dataclasses import dataclass, field

REASONS = [
    "TREND_FAILURE", "MARKET_DETERIORATION", "NEWS_EVENT", "STOP_TOO_TIGHT",
    "ENTRY_TOO_EARLY", "PROFIT_TARGET_TOO_CONSERVATIVE", "EXIT_SCORE_DETERIORATION",
]


@dataclass
class AttributionResult:
    primary_reason: str
    confidence: str          # "high" (had a real MAE/MFE match) | "low" (pattern-only fallback)
    evidence: list = field(default_factory=list)


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("trade_attribution", {}) or {}


def _match_mae_mfe(pattern: dict, mae_mfe_rows: list) -> dict | None:
    """Best-effort join: same ticker, outcome_pct within 0.5pp of the
    pattern's recorded outcome. There's no shared trade_id guaranteed
    between pattern_database and mae_mfe_data (different close paths can
    populate each independently - see engine/mae_mfe_engine.py and
    scheduler.py's _close_due_patterns), so this is a heuristic match, not a
    guaranteed-correct join - documented via the "low" confidence label when
    no match is found at all."""
    ticker = pattern.get("ticker")
    outcome = pattern.get("outcome_pct")
    if ticker is None or outcome is None:
        return None
    best, best_diff = None, None
    for row in mae_mfe_rows:
        if row.get("ticker") != ticker or row.get("outcome_pct") is None:
            continue
        diff = abs(row["outcome_pct"] - outcome)
        if diff <= 0.5 and (best_diff is None or diff < best_diff):
            best, best_diff = row, diff
    return best


def classify_trade(pattern: dict, mae_mfe_row: dict = None, cfg: dict = None) -> AttributionResult:
    """
    pattern: one row from db.get_patterns(closed_only=True) - {ticker, features,
        outcome_pct, hold_hours, exit_reason, ...}.
    mae_mfe_row: OPTIONAL matching row from db.get_recent_mae_mfe() (real
        closed trades only) - {mae_pct, mfe_pct, outcome_pct, hold_hours, ...}.
        None is tolerated (falls back to a coarser pattern-only rule set).
    """
    tcfg = _cfg(cfg)
    outcome = pattern.get("outcome_pct") or 0.0
    hold_hours = pattern.get("hold_hours") or (mae_mfe_row or {}).get("hold_hours") or 0.0
    features = pattern.get("features", {}) or {}
    evidence = []

    if mae_mfe_row is not None:
        mae = mae_mfe_row.get("mae_pct") or 0.0
        mfe = mae_mfe_row.get("mfe_pct") or 0.0
        confidence = "high"

        stop_mfe_min = tcfg.get("stop_too_tight_mfe_min_pct", 2.0)
        stop_ratio = tcfg.get("stop_too_tight_mfe_to_loss_ratio", 1.5)
        if outcome <= 0 and mfe >= stop_mfe_min and mfe >= abs(outcome) * stop_ratio:
            evidence.append(f"MFE {mfe:.1f}% >= {stop_ratio}x the eventual loss ({outcome:.1f}%) - "
                             f"price moved in favor before reversing to a loss")
            return AttributionResult("STOP_TOO_TIGHT", confidence, evidence)

        early_mae_min = tcfg.get("entry_too_early_mae_min_pct", 4.0)
        early_mfe_max = tcfg.get("entry_too_early_mfe_max_pct", 1.5)
        if outcome <= 0 and mae >= early_mae_min and mfe < early_mfe_max:
            evidence.append(f"MAE {mae:.1f}% with MFE only {mfe:.1f}% - went against entry almost "
                             f"immediately, never worked in favor")
            return AttributionResult("ENTRY_TOO_EARLY", confidence, evidence)

        cons_ratio = tcfg.get("conservative_target_mfe_ratio", 1.5)
        cons_gap = tcfg.get("conservative_target_min_gap_pct", 2.0)
        if outcome > 0 and mfe >= outcome * cons_ratio and (mfe - outcome) >= cons_gap:
            evidence.append(f"MFE {mfe:.1f}% vs realized {outcome:.1f}% - exited well short of the "
                             f"trade's actual favorable excursion")
            return AttributionResult("PROFIT_TARGET_TOO_CONSERVATIVE", confidence, evidence)

    else:
        confidence = "low"
        evidence.append("No matching mae_mfe_data row (simulated/time-based close, or pre-Phase-3 trade) - "
                         "classified from pattern features only")

    news_move = tcfg.get("news_event_min_abs_move_pct", 6.0)
    news_hold = tcfg.get("news_event_max_hold_hours", 24)
    if outcome <= 0 and abs(outcome) >= news_move and hold_hours <= news_hold:
        evidence.append(f"{outcome:.1f}% move in <= {news_hold}h - too fast/large for normal technical "
                         f"drift, consistent with a news/event shock")
        return AttributionResult("NEWS_EVENT", confidence, evidence)

    trend_min_hold = tcfg.get("trend_failure_min_hold_hours", 48)
    regime = features.get("regime", "unknown")
    if outcome <= 0 and hold_hours >= trend_min_hold:
        evidence.append(f"Held {hold_hours:.0f}h (>= {trend_min_hold}h) before closing at a loss - a "
                         f"slow bleed, not a shock; entry regime was {regime}")
        return AttributionResult("TREND_FAILURE", confidence, evidence)

    if outcome <= 0 and regime in ("BEAR", "CRISIS", "CHOPPY"):
        evidence.append(f"Entry regime was {regime} - broader market conditions were already unfavorable")
        return AttributionResult("MARKET_DETERIORATION", confidence, evidence)

    evidence.append(f"outcome {outcome:.1f}% over {hold_hours:.0f}h did not match a sharper failure "
                     f"pattern - default to gradual technical deterioration")
    return AttributionResult("EXIT_SCORE_DETERIORATION", confidence, evidence)


def attribute_all(db, mode: str = "SWING", cfg: dict = None) -> dict:
    """Classifies every closed pattern_database row for `mode` and returns a
    summary: counts + win rate per reason, so a human can see e.g. "60% of
    your losses this month were STOP_TOO_TIGHT" at a glance. Mirrors
    analytics/performance.py's win_rate_by() output shape."""
    patterns = db.get_patterns(mode=mode, closed_only=True)
    mae_mfe_rows = db.get_recent_mae_mfe(limit=1000)

    per_reason = {r: [] for r in REASONS}
    classified = []
    for p in patterns:
        match = _match_mae_mfe(p, mae_mfe_rows)
        result = classify_trade(p, match, cfg)
        per_reason[result.primary_reason].append(p.get("outcome_pct") or 0.0)
        classified.append({
            "ticker": p.get("ticker"), "outcome_pct": p.get("outcome_pct"),
            "hold_hours": p.get("hold_hours"), "reason": result.primary_reason,
            "confidence": result.confidence, "evidence": result.evidence,
        })

    summary = {}
    for reason, outcomes in per_reason.items():
        n = len(outcomes)
        wins = sum(1 for o in outcomes if o > 0)
        summary[reason] = {
            "n": n,
            "win_rate": round(wins / n, 3) if n else None,
            "avg_outcome_pct": round(sum(outcomes) / n, 2) if n else None,
        }

    return {"mode": mode, "n_trades": len(patterns), "by_reason": summary, "trades": classified}
