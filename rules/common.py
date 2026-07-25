"""Shared small types used across rules/ and engine/rules_engine.py."""
from dataclasses import dataclass, field
from typing import Any

# ── Exit vocabulary (§50, Phase 2.5) ────────────────────────────────────────
#
# pattern_database.exit_reason is a human-readable sentence and stays one - it
# is what the UI shows and what analytics/regret_analysis.py narrates. What it
# cannot do is be counted. The stop-loss exits recorded before this change read
#
#     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $82.56 <= stop $83.15
#     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $22.13 <= stop $22.39
#
# - four stops, four distinct strings, because the price is interpolated into
# the reason. GROUP BY exit_reason returns one row per trade, so
# engine/ev_engine.py's p_stop_loss cannot filter on it and says so in its
# HONESTY NOTE.
#
# exit_kind is the countable companion: a closed set, indexed, written beside
# the sentence rather than replacing it.
EXIT_KINDS = frozenset({
    "stop_loss",       # initial-risk or dynamic stop hit
    "trailing_stop",   # trailing stop hit after the position moved in favour
    "take_profit",     # target reached
    "time_stop",       # horizon / pattern_hold_days expiry
    "eod_flatten",     # DAY position closed at the session cutoff
    "rule_exit",       # a sell_rules signal that is none of the above
    "manual",          # confirm_fill.py, human-confirmed
    "rotation",        # closed to make room for a higher-conviction candidate
})


def classify_exit(exit_reason: str) -> str | None:
    """Map an exit_reason to an EXIT_KINDS member, or None when the string does
    not carry enough structure to say.

    Returning None is a real answer here, not a failure. A wrong exit_kind is
    strictly worse than a missing one: the whole point of the column is that
    something can be counted on it, and a bucket half-filled by guesswork is
    the kind of number that gets trusted precisely because it looks complete.
    Consumers filter `exit_kind IS NOT NULL`.

    WHAT THIS IS AND IS NOT PARSING. The reasons this classifies are namespaced
    tokens that were generated FROM a structured value and never contained
    prose in the first place - scheduler.py's price watch builds
    `price_watch:{reason.split(' ')[0]}` from check_exit_triggers()'s fixed
    vocabulary, and rotation/time/manual closes pass fixed literals. Reading
    those back is reading a token, not re-deriving meaning from a sentence.

    What it deliberately does NOT do is classify `sell_rules:...`, which is
    genuinely free text assembled per-trade by rules/sell_rules.py and
    engine/stop_state_machine.py. Several of those ARE stops and it is tempting
    to prefix-match "Dynamic stop hit" - don't. The fix is to give sell_rules a
    structured exit code at the point of decision (Phase 3), not to grow a
    string-matching table here that silently drifts from its producers.

    The book prefix (`paper_` / `live_`) is stripped first: which book a trade
    was in is already recorded on the row, and it is not a kind of exit.
    """
    if not exit_reason:
        return None
    r = str(exit_reason).strip()
    for book_prefix in ("paper_", "live_"):
        if r.startswith(book_prefix):
            r = r[len(book_prefix):]
            break

    if r.startswith("price_watch:"):
        token = r.split(":", 1)[1].split(" ")[0].strip()
        return token if token in EXIT_KINDS else None

    if r.startswith("rotation"):
        return "rotation"
    if r.startswith("eod_flatten") or r.startswith("loop_b_urgent:EOD FLATTEN"):
        return "eod_flatten"
    if r == "time_based_close":
        return "time_stop"
    if r == "manual_fill_confirmed":
        return "manual"

    # sell_rules:, loop_b_urgent: (other labels), seeded_from_real_portfolio,
    # and anything a future caller invents. Unclassified on purpose.
    return None


@dataclass
class RuleResult:
    name: str
    passed: bool
    weight: float = 0.0
    value: Any = None
    detail: str = ""


@dataclass
class Position:
    ticker: str
    entry_price: float
    entry_time: str
    shares: float
    dollar_amount: float
    highest_price: float
    current_price: float
    unrealized_pnl: float
    stop_loss: float = None
    take_profit: float = None
    trailing_high: float = None
    status: str = "open"

    @classmethod
    def from_db_row(cls, row: dict) -> "Position":
        return cls(
            ticker=row["ticker"], entry_price=row["entry_price"], entry_time=row["entry_time"],
            shares=row["shares"], dollar_amount=row["dollar_amount"], highest_price=row["highest_price"],
            current_price=row["current_price"], unrealized_pnl=row["unrealized_pnl"],
            stop_loss=row.get("stop_loss"), take_profit=row.get("take_profit"),
            trailing_high=row.get("trailing_high"), status=row.get("status", "open"),
        )

    @property
    def pnl_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100
