"""Risk guardrails + kill switch. Used by engine/executor.py's safety chain -
these are per-trade / per-day account-level checks, distinct from market_filters.py
(which are market-wide) and buy/sell_rules.py (which are per-ticker signal rules)."""
from dataclasses import dataclass

import yaml

from config_loader import CONFIG_PATH
from rules.common import RuleResult


def check_kill_switch(cfg) -> RuleResult:
    ok = not cfg.risk.kill_switch_triggered
    return RuleResult("kill_switch", ok, detail="ACTIVE - trading halted" if not ok else "off")


def check_max_trades_per_day(cfg, trades_today: int) -> RuleResult:
    ok = trades_today < cfg.risk.max_trades_per_day
    return RuleResult("max_trades_per_day", ok, value=trades_today,
                       detail=f"{trades_today}/{cfg.risk.max_trades_per_day} trades today")


def check_max_daily_loss(cfg, realized_pnl_today: float) -> RuleResult:
    ok = realized_pnl_today > -abs(cfg.risk.max_daily_loss_usd)
    return RuleResult("max_daily_loss", ok, value=realized_pnl_today,
                       detail=f"today P&L ${realized_pnl_today:.2f} vs -${cfg.risk.max_daily_loss_usd}")


def check_buying_power(dollar_amount: float, buying_power: float) -> RuleResult:
    ok = buying_power is not None and buying_power >= dollar_amount
    return RuleResult("sufficient_buying_power", ok, value=buying_power,
                       detail=f"buying power ${buying_power} vs needed ${dollar_amount}")


def check_position_limits(cfg, open_positions_count: int) -> RuleResult:
    ok = open_positions_count < cfg.trading.max_positions
    return RuleResult("max_positions", ok, value=open_positions_count,
                       detail=f"{open_positions_count}/{cfg.trading.max_positions} positions open")


def check_position_size_limit(cfg, dollar_amount: float) -> RuleResult:
    ok = dollar_amount <= cfg.risk.max_position_size_usd
    return RuleResult("max_position_size", ok, value=dollar_amount,
                       detail=f"${dollar_amount} <= ${cfg.risk.max_position_size_usd}")


@dataclass
class RiskCheckResult:
    can_trade: bool
    reason: str = "OK"


class LegacyRiskEngine:
    """Dot-access (SimpleNamespace cfg) version used only by the legacy
    engine/executor.py automated-order-placement path, which is not part of the
    active free/MCP-SDK flow (Robinhood is never called from Python there -
    see README.md). Kept for anyone who wires that path back in."""

    def check(self, daily_stats: dict, cfg) -> RiskCheckResult:
        if cfg.risk.kill_switch_triggered:
            return RiskCheckResult(False, "kill switch active")

        trades_today = daily_stats.get("trades_placed", 0)
        if trades_today >= cfg.risk.max_trades_per_day:
            return RiskCheckResult(False, f"{trades_today}/{cfg.risk.max_trades_per_day} trades today")

        realized = daily_stats.get("realized_pnl", 0.0)
        if realized <= -abs(cfg.risk.max_daily_loss_usd):
            return RiskCheckResult(False, f"daily loss ${realized:.2f} breached -${cfg.risk.max_daily_loss_usd}")

        return RiskCheckResult(True, "OK")


class RiskEngine:
    """Dict-access `cfg` version used by the ACTIVE scheduler.py flow.
    Constructed once per cycle as RiskEngine(db, cfg); .check() takes no args
    and returns a plain dict ({"can_trade": bool, "reason": str}) matching
    scheduler.py's `risk_check["can_trade"]` / `risk_check["reason"]` usage."""

    def __init__(self, db, cfg: dict):
        self.db = db
        self.cfg = cfg

    def check(self) -> dict:
        cfg = self.cfg
        if cfg["risk"]["kill_switch_triggered"]:
            return {"can_trade": False, "reason": "Kill switch is ON"}

        stats = self.db.get_daily_stats()
        trades_today = stats.get("trades_placed", 0)
        if trades_today >= cfg["risk"]["max_trades_per_day"]:
            return {"can_trade": False,
                    "reason": f"{trades_today}/{cfg['risk']['max_trades_per_day']} trades today"}

        realized = stats.get("realized_pnl", 0.0)
        if realized <= -abs(cfg["risk"]["max_daily_loss_usd"]):
            return {"can_trade": False,
                    "reason": f"daily loss ${realized:.2f} breached -${cfg['risk']['max_daily_loss_usd']}"}

        return {"can_trade": True, "reason": "OK"}


def trip_kill_switch_if_needed(db, cfg) -> bool:
    """Auto-sets kill_switch_triggered=true in config.yaml (persisted to disk) if the
    daily loss limit has been breached. Per build note: kill switch requires manual
    restart + config edit to clear - this function only ever flips it ON, never off."""
    realized = db.realized_pnl_today()
    if realized <= -abs(cfg.risk.max_daily_loss_usd) and not cfg.risk.kill_switch_triggered:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f)
        raw["risk"]["kill_switch_triggered"] = True
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        db.set_kill_switch(True)
        db.log("CRITICAL", f"KILL SWITCH TRIPPED - daily loss ${realized:.2f} breached limit")
        return True
    return False
