"""Shared small types used across rules/ and engine/rules_engine.py."""
from dataclasses import dataclass, field
from typing import Any


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
