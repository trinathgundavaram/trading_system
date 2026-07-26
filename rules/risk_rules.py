"""Risk guardrails + kill switch: per-trade / per-day ACCOUNT-level checks,
distinct from market_filters.py (which are market-wide) and buy/sell_rules.py
(which are per-ticker signal rules).

§54 (Phase 2.5) removed a second, parallel implementation of these limits from
this file: six module-level `check_*(cfg, ...)` helpers returning RuleResult,
and a `LegacyRiskEngine` class written against the dot-access SimpleNamespace
config. All seven had ZERO call sites - not in the engine, not in the
scheduler, not in the tests - while the limits they described were, and are,
genuinely enforced elsewhere:

    max_position_size_usd  -> engine/position_sizing.py (clamp) and
                              engine/live_trader.py (clamp)
    max_positions          -> engine/paper_trader.py and engine/live_trader.py
    max_trades_per_day     -> RiskEngine.check(), below
    max_daily_loss         -> RiskEngine.check() + trip_kill_switch_if_needed()
    kill_switch            -> RiskEngine.check() + rules/hard_vetoes.py
    buying_power           -> the paper purse check in engine/paper_trader.py;
                              real buying power is Robinhood's to enforce

Two implementations of one limit is not redundancy, it is a coin flip about
which one a future edit lands in - and they had already diverged. The dead copy
compared trades_today with `>=` where the live one uses `>`, and read
`max_daily_loss_usd` raw where the live path uses §8's daily_loss_limit(),
which is the tighter of the absolute cap and a percentage of equity. Anyone
reviving engine/executor.py against the dead copy would have been reviving the
pre-§8 limits while believing otherwise. Git has them if that path ever comes
back; rewriting against the live definitions is the correct way to do it.
"""
import json
import logging
import re
import os
from datetime import datetime

import yaml

from config_loader import CONFIG_PATH

logger = logging.getLogger("trading")


def _default_simulated(cfg: dict) -> bool:
    """Which book is this deployment actually trading?

    WATCH (or anything that is not EXECUTE) means the paper book is the one
    placing trades, so it is the one whose budget matters. Defaulting to the
    LIVE counters during a paper session is how the daily limits came to read
    zero forever (§7)."""
    return str((cfg.get("trading", {}) or {})
               .get("watch_execute", "WATCH")).upper() != "EXECUTE"


def daily_loss_limit(db, cfg: dict, simulated: bool) -> float:
    """Today's loss limit as a POSITIVE dollar figure: the TIGHTER of the
    absolute cap and a percentage of the book's actual equity (§8).

    max_daily_loss_usd was $500 against a $1,000 paper account. A limit that
    only triggers after losing half the account is not a limit - it is a
    number. Expressed as a percentage it resolves to about $20 on that
    account, which against the observed -$15.25 over four days would have
    halted the session on day one and forced the question weeks earlier.

    Falls back to the absolute cap when equity is unknown or the percentage is
    unset, so an unreadable account can never widen the limit."""
    risk = cfg.get("risk", {}) or {}
    absolute = abs(float(risk.get("max_daily_loss_usd", 500) or 500))
    pct = float(risk.get("max_daily_loss_pct", 0) or 0)
    if pct <= 0:
        return absolute
    try:
        # Equity = cash + what is deployed, valued at COST.
        #
        # Deliberately cost basis, not market value. Market value needs a
        # current price per position, and this runs on every buy - an
        # unpriced position would contribute 0 and silently understate equity,
        # which TIGHTENS the limit. A fully-invested $1,000 paper account with
        # $100 cash would resolve to a $2 daily stop and halt the session for
        # the wrong reason. Cost basis needs no quotes, cannot collapse to
        # zero while positions are open, and is close enough for sizing a
        # limit that is already a round percentage.
        deployed = sum(float(p.get("dollar_amount") or 0)
                       for p in db.get_all_positions(simulated=simulated))
        if simulated:
            acct = db.get_paper_account() or {}
            equity = float(acct.get("cash", 0) or 0) + deployed
        else:
            # P1-08 (audit finding, external review 2026-07-26): this used to
            # be `equity = deployed` - live equity with NO cash added, while
            # the paper branch above adds it. That is not a deliberate
            # asymmetry, just an unfinished one: the paper book's cash is a
            # free local DB read, while live cash needs a Robinhood API call,
            # and that call was never wired in here.
            #
            # engine.live_trader.account_cash() makes that same call (reusing
            # the login/account-number plumbing _buying_power() already uses
            # before every live order) and returns None on anything short of
            # a clean read - no credentials, login failure, network error -
            # in which case this falls back to the exact pre-fix figure
            # (deployed only). So a reachable account gets the correct,
            # larger equity figure (a WIDER, more accurate limit); an
            # unreachable one is no worse off than before.
            live_cash = None
            try:
                from engine.live_trader import account_cash
                live_cash = account_cash(cfg)
            except Exception as e:
                logger.warning(f"daily_loss_limit: live cash read failed: {e}")
            equity = deployed + live_cash if live_cash is not None else deployed
    except Exception:
        return absolute
    return min(absolute, equity * pct / 100.0) if equity > 0 else absolute


def drawdown_breach(db, cfg: dict, simulated: bool) -> str:
    """Reason string when a drawdown cap is breached, "" when it is not (§11).

    Two caps, watching two different things:

      max_intraday_drawdown_pct - today's worst peak-to-trough. Halts the
          session. This is the daily circuit breaker, and realised P&L alone
          cannot see what it sees: an account can round-trip 4% intraday and
          finish flat, having taken every bit of that risk.
      max_running_drawdown_pct  - distance from the all-time equity high.
          Halts entirely. That is not a bad day, it is the strategy having
          stopped working, and it is not a decision to re-take automatically
          tomorrow morning.

    Reads the drawdown columns for the requested BOOK, so a paper session
    cannot be halted by a live figure or the reverse.

    Both caps default to 0 = off, and an unreadable stats row returns "" - an
    unknown drawdown must not block trading. That is the opposite of
    daily_loss_limit()'s default, deliberately: there, failing open WIDENS a
    limit that already exists and is already known, whereas failing closed
    here would halt the session on the strength of a number that nothing has
    written yet.
    """
    # Running first: it is the more serious of the two, so when both are
    # breached it is the one that should be named in the halt reason.
    return (_running_drawdown_breach(db, cfg, simulated)
            or _intraday_drawdown_breach(db, cfg, simulated))


def _read_drawdown(db, simulated: bool) -> dict:
    """Today's drawdown figures for one book, or {} when unreadable.

    The book prefix is the §7 property applied to a third pair of columns: a
    live figure must never halt a paper session, or the reverse.
    """
    try:
        stats = db.get_daily_stats() or {}
    except Exception as e:
        logger.warning(f"drawdown check: could not read daily_stats: {e}")
        return {}
    prefix = "paper_" if simulated else ""
    return {"intraday": float(stats.get(f"{prefix}max_drawdown", 0) or 0),
            "running": float(stats.get(f"{prefix}running_drawdown", 0) or 0)}


def _intraday_drawdown_breach(db, cfg: dict, simulated: bool) -> str:
    cap = float((cfg.get("risk", {}) or {}).get("max_intraday_drawdown_pct", 0) or 0)
    if not cap:
        return ""
    dd = _read_drawdown(db, simulated).get("intraday")
    if dd is None or dd < cap:
        return ""
    return f"intraday drawdown {dd:.2f}% >= {cap}% - no new entries today"


def _running_drawdown_breach(db, cfg: dict, simulated: bool) -> str:
    """Separate from the intraday check because it has a separate consequence:
    trip_kill_switch_if_needed() escalates THIS one to the kill switch, and
    deliberately not the intraday one."""
    cap = float((cfg.get("risk", {}) or {}).get("max_running_drawdown_pct", 0) or 0)
    if not cap:
        return ""
    dd = _read_drawdown(db, simulated).get("running")
    if dd is None or dd < cap:
        return ""
    return (f"running drawdown {dd:.2f}% >= {cap}% "
            f"- halted pending human review")


class RiskEngine:
    """Dict-access `cfg` version used by the ACTIVE scheduler.py flow.
    Constructed as RiskEngine(db, cfg[, simulated]); .check() takes no args and
    returns a plain dict ({"can_trade": bool, "reason": str}) matching
    scheduler.py's `risk_check["can_trade"]` / `risk_check["reason"]` usage.

    BOOK-AWARE as of §7. It previously read `trades_placed` and `realized_pnl`
    unconditionally - the LIVE columns - which on a paper-only deployment are
    both permanently zero. The result was a risk engine that answered
    can_trade: True to every question it was ever asked.
    """

    def __init__(self, db, cfg: dict, simulated: bool = None):
        self.db = db
        self.cfg = cfg
        self.simulated = _default_simulated(cfg) if simulated is None else simulated

    def check(self) -> dict:
        cfg = self.cfg
        risk = cfg.get("risk", {}) or {}
        if risk.get("kill_switch_triggered"):
            return {"can_trade": False, "reason": "Kill switch is ON"}

        book = "paper" if self.simulated else "live"

        # A config with no `risk` section is a BROKEN config, not a config with
        # no limits. This method was half-defensive: the kill-switch read above
        # used .get() and the very next line indexed cfg["risk"] directly, so a
        # missing section raised KeyError from inside execute_buy - where
        # scheduler.py's try/except logs it as "paper buy failed". A risk
        # misconfiguration presenting as a buy failure is the wrong diagnosis
        # on the wrong line.
        #
        # Fails CLOSED and says why. The alternative - .get() with a default
        # limit - would let a truncated or hand-edited config silently
        # substitute limits nobody chose, which is the failure mode §8 exists
        # to prevent.
        if "max_trades_per_day" not in risk:
            return {"can_trade": False,
                    "reason": "config has no risk.max_trades_per_day - refusing "
                              "to trade against limits nobody set"}

        trades_today = self.db.trades_placed_today(self.simulated)
        max_trades = int(risk["max_trades_per_day"])
        if trades_today >= max_trades:
            return {"can_trade": False,
                    "reason": f"{trades_today}/{max_trades} {book} trades today"}

        realized = self.db.realized_pnl_today(simulated=self.simulated)
        limit = daily_loss_limit(self.db, cfg, self.simulated)
        if realized <= -limit:
            return {"can_trade": False,
                    "reason": f"{book} daily loss ${realized:.2f} breached -${limit:.2f}"}

        dd = drawdown_breach(self.db, cfg, self.simulated)
        if dd:
            return {"can_trade": False, "reason": f"{book} {dd}"}

        return {"can_trade": True, "reason": "OK"}


_KILL_LINE = re.compile(r"^(\s*)kill_switch_triggered:\s*(?:false|False|no|off)\s*$",
                        re.MULTILINE)
_KILL_TRUE = re.compile(r"^\s*kill_switch_triggered:\s*(?:true|True|yes|on)\s*$",
                        re.MULTILINE)


def _persist_kill_switch(reason: str):
    """Flip kill_switch_triggered to true in config.yaml, atomically, WITHOUT
    reserialising the file.

    A yaml.safe_dump round-trip would strip every comment in config.yaml - and
    that file is where the reasoning behind each risk threshold is recorded.
    Destroying the documentation at the exact moment the system halts itself,
    when the next thing a human does is open that file to understand why, is a
    bad trade. So this is a targeted single-line substitution, with a
    round-trip only as the fallback for a config that does not match the
    expected shape.

    The atomic replace is not incidental either: config.yaml is re-read on
    every call by design (hot reload), so a partially-written file during a
    loss event would crash the scheduler at precisely the moment you need it
    to stop trading in an orderly way.
    """
    text = CONFIG_PATH.read_text()
    # json.dumps, NOT yaml.safe_dump. safe_dump serialises a bare scalar as a
    # whole DOCUMENT and appends "...", YAML's end-of-document marker - which
    # lands in the middle of the file and truncates everything after it at
    # parse time. A JSON string is a valid YAML double-quoted scalar and has
    # no such framing. (Caught by test_persist_preserves_config_comments; the
    # first hand-check of this missed it because it only inspected keys that
    # happened to sit BEFORE the injected marker.)
    stamped = f"  kill_switch_reason: {json.dumps(reason)}\n"

    new, n = _KILL_LINE.subn(
        lambda m: f"{m.group(1)}kill_switch_triggered: true", text, count=1)

    if n == 0 and _KILL_TRUE.search(text):
        # Already true. Not the normal path - trip_kill_switch_if_needed()
        # returns early in that case - but reachable if something calls this
        # directly, and it must NOT fall through to the reserialising branch
        # below just because there was nothing to flip.
        new, n = text, 1

    if n == 1:
        # Drop any previous reason line, then record this one next to the flag.
        new = re.sub(r"^\s*kill_switch_reason:.*\n", "", new, flags=re.MULTILINE)
        new = _KILL_TRUE.sub(lambda m: m.group(0) + "\n" + stamped.rstrip("\n"),
                             new, count=1)
        if not new.endswith("\n"):
            new += "\n"
    else:
        # The key is absent entirely - a hand-edited or truncated config. Fall
        # back to a structural write rather than silently not persisting, and
        # say plainly what it costs.
        disk = yaml.safe_load(text) or {}
        disk.setdefault("risk", {})["kill_switch_triggered"] = True
        disk["risk"]["kill_switch_reason"] = reason
        new = yaml.safe_dump(disk, sort_keys=False)
        logger.warning("kill switch: config.yaml has no kill_switch_triggered "
                       "key - rewrote it structurally, COMMENTS ARE LOST")

    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(new)
    os.replace(tmp, CONFIG_PATH)


def trip_kill_switch_if_needed(db, cfg=None, simulated: bool = None) -> bool:
    """Auto-arm the kill switch on a realised-loss OR running-drawdown breach.

    ONLY EVER FLIPS ON. Clearing it stays a deliberate human act (edit
    config.yaml, restart), which is correct: the single most dangerous failure
    mode for an automatic breaker is one that resets itself - a bad day would
    re-arm the moment the clock rolled over.

    §9 (Phase 2) rewrote this. The previous version was broken in three
    independent ways and had zero call sites, so none of them had ever
    surfaced:
      1. `db.realized_pnl_today()` did not exist - AttributeError on line one.
      2. `db.set_kill_switch()` did not exist either.
      3. It used attribute access (`cfg.risk.max_daily_loss_usd`) on what the
         active scheduler passes as a plain dict.
    engine/rules_catalog.py meanwhile told the operator it "runs every cycle".

    §11 (Phase 2) added the second trigger. The RUNNING drawdown cap is the
    only one of the two drawdown limits that belongs here: an intraday breach
    is a statement about today, and drawdown_breach() already blocks new
    entries for the rest of it, so escalating that to a switch a human must
    clear would halt tomorrow for something that happened this afternoon.
    A running breach is a statement about the strategy - 15% off the all-time
    high is not a bad day - and that decision should not be re-taken
    automatically at the next equity point that happens to tick up. Routing it
    through the kill switch is what makes "human review required" true rather
    than merely printed.

    The INTRADAY cap deliberately stays a gate-only control. Together they
    give the two halts different half-lives, which is the point.

    Returns True only when it actually tripped on this call.
    """
    raw = cfg if isinstance(cfg, dict) else None
    if raw is None:
        from config_loader import load_config_dict
        raw = load_config_dict()

    risk = raw.get("risk", {}) or {}
    if risk.get("kill_switch_triggered"):
        return False                      # already on; nothing to do, no rewrite

    if simulated is None:
        simulated = _default_simulated(raw)

    book = "paper" if simulated else "live"
    realized = db.realized_pnl_today(simulated=simulated)
    limit = daily_loss_limit(db, raw, simulated)

    if realized <= -limit:
        reason = (f"AUTO {datetime.utcnow().isoformat()}Z: {book} realised "
                  f"${realized:.2f} breached -${limit:.2f}")
    else:
        dd = _running_drawdown_breach(db, raw, simulated)
        if not dd:
            return False
        reason = f"AUTO {datetime.utcnow().isoformat()}Z: {book} {dd}"

    # Persist to config.yaml so a process restart does NOT clear it.
    #
    # The atomic write is not incidental. config.yaml is re-read on every call
    # by design (hot reload), so a partially-written file during a loss event
    # would crash the scheduler at precisely the moment you need it to stop
    # trading in an orderly way.
    try:
        _persist_kill_switch(reason)
    except Exception as e:
        # A failed persist must not swallow the event. Keep going: the
        # in-memory flag, the DB row and the alert still fire, and a loud log
        # line is better than a silent half-trip.
        logger.error(f"kill switch: could not persist to config.yaml: {e}")

    # Reflect it in the caller's live dict too, so the current cycle stops
    # immediately rather than at the next config reload.
    risk["kill_switch_triggered"] = True
    risk["kill_switch_reason"] = reason

    try:
        db.set_kill_switch(True, reason=reason)
        db.log_ui_event("kill_switch_auto",
                        {"realized": realized, "limit": limit, "book": book})
    except Exception as e:
        logger.error(f"kill switch: could not record to the database: {e}")

    # An automatic kill switch that trips silently at 2pm while you are out is
    # only half a control.
    try:
        from engine.notifications import send_critical
        send_critical("TRADING HALTED", reason)
    except Exception as e:
        logger.error(f"kill switch fired but notification failed: {e}")

    logger.critical(f"KILL SWITCH TRIPPED - {reason}")
    return True
