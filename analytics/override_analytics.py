"""Tracks every time a human overrides the system's recommendation (approve a
signal it wanted to skip, deny one it liked, resize, move a stop, exit early),
and whether that override helped or hurt versus what the system would have done."""
import json
import uuid


def record_override(db, signal_id, override_type: str, system_recommendation: dict,
                     user_action: dict) -> str:
    override_id = str(uuid.uuid4())
    db.record_override(override_id, signal_id, override_type,
                        json.dumps(system_recommendation), json.dumps(user_action))
    return override_id


def close_override(db, override_id: str, outcome_pct: float, system_would_have_pct: float):
    db.close_override_outcome(override_id, outcome_pct, system_would_have_pct)


def override_impact_report(db) -> dict:
    overrides = db.get_overrides(limit=1000)
    closed = [o for o in overrides if o.get("outcome_pct") is not None]
    if not closed:
        return {"n": 0, "message": "no closed overrides yet"}

    improved = sum(1 for o in closed if o["override_improved"])
    avg_user = sum(o["outcome_pct"] for o in closed) / len(closed)
    avg_system = sum(o["system_would_have_pct"] for o in closed) / len(closed)

    by_type = {}
    for o in closed:
        by_type.setdefault(o["override_type"], []).append(o)

    by_type_summary = {
        t: {
            "n": len(rows),
            "pct_improved": round(sum(1 for r in rows if r["override_improved"]) / len(rows) * 100, 1),
            "avg_outcome_pct": round(sum(r["outcome_pct"] for r in rows) / len(rows), 2),
        }
        for t, rows in by_type.items()
    }

    return {
        "n": len(closed),
        "pct_overrides_improved": round(improved / len(closed) * 100, 1),
        "avg_user_outcome_pct": round(avg_user, 2),
        "avg_system_would_have_pct": round(avg_system, 2),
        "by_type": by_type_summary,
    }
