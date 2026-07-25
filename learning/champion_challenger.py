"""Champion/Challenger: run a proposed rule-weight config in parallel (watch
mode - it never places its own trades) against the live "champion" config, and
use a two-proportion z-test to decide if the challenger's win rate is really
different before ever promoting it."""
import json
import uuid
from datetime import datetime

from analytics.confidence_intervals import two_proportion_z_test


class ChampionChallenger:
    def __init__(self, db, cfg: dict):
        self.db = db
        self.cfg = cfg

    def start_challenge(self, challenger_config: dict) -> str:
        challenge_id = str(uuid.uuid4())
        self.db.create_challenge(challenge_id, json.dumps(challenger_config))
        return challenge_id

    def record_trade(self, challenge_id: str, is_challenger: bool, won: bool, pnl_pct: float):
        self.db.record_challenge_trade(challenge_id, is_challenger, won, pnl_pct)

    def evaluate(self, challenge_id: str) -> dict:
        row = self.db.get_challenge(challenge_id)
        if not row:
            return {"error": "challenge not found"}

        min_trades = self.cfg["learning"].get("champion_challenger_min_trades_for_significance", 30)
        champ_n, chal_n = row["champion_trades"], row["challenger_trades"]

        if champ_n < min_trades or chal_n < min_trades:
            return {
                "challenge_id": challenge_id, "ready": False,
                "champion_trades": champ_n, "challenger_trades": chal_n,
                "min_required": min_trades,
                "message": f"Need {min_trades} trades on each side (have {champ_n} champion, {chal_n} challenger)",
            }

        test = two_proportion_z_test(row["champion_wins"], champ_n, row["challenger_wins"], chal_n)
        champ_win_rate = row["champion_wins"] / champ_n
        chal_win_rate = row["challenger_wins"] / chal_n
        champ_avg_pnl = row["champion_pnl_pct"] / champ_n
        chal_avg_pnl = row["challenger_pnl_pct"] / chal_n

        recommendation = "hold"
        if test["significant"] and chal_win_rate > champ_win_rate and chal_avg_pnl > champ_avg_pnl:
            recommendation = "promote_challenger"
        elif test["significant"] and chal_win_rate < champ_win_rate:
            recommendation = "discard_challenger"

        self.db.update_challenge_status(challenge_id, row["status"], test["p_value"])

        return {
            "challenge_id": challenge_id, "ready": True,
            "champion_win_rate": champ_win_rate, "challenger_win_rate": chal_win_rate,
            "champion_avg_pnl_pct": champ_avg_pnl, "challenger_avg_pnl_pct": chal_avg_pnl,
            "z_test": test, "recommendation": recommendation,
            "note": "Recommendation only - promoting/discarding is a manual config.yaml edit.",
        }

    def promote(self, challenge_id: str):
        self.db.update_challenge_status(challenge_id, "promoted")

    def discard(self, challenge_id: str):
        self.db.update_challenge_status(challenge_id, "discarded")
