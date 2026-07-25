"""§48/§52/§53/§54 (Phase 2.5): entry ATR units, the reset epoch, the
calibration arithmetic, and the removal of the writerless risk flags.

The thread running through these: a risk control is only real if the quantity
it compares is the quantity it claims to compare, and if something writes the
flag it reads.
"""
import pytest

from engine.portfolio_risk import _position_atr_pct, _position_risk_band_pct


# ── §53: the units ──────────────────────────────────────────────────────────

def test_persisted_atr_is_preferred_over_the_proxy():
    """The candidate side has always passed atr/price*100. This side used to
    pass stop distance. Same threshold, two quantities."""
    pos = {"ticker": "X", "entry_price": 100.0, "current_stop_price": 95.0,
           "entry_atr_pct": 7.0}
    assert _position_risk_band_pct(pos) == pytest.approx(5.0)   # the old answer
    assert _position_atr_pct(pos) == pytest.approx(7.0)         # the true one


def test_the_clamp_is_why_the_proxy_read_low():
    """scheduler.py seeds risk_per_share = min(max(1.2*ATR, price*1.5%),
    price*stop_loss_pct). Past a certain volatility the stop is clamped by
    stop_loss_swing_pct while ATR keeps going, so the proxy saturates - a
    7%-ATR name behind a 5%-clamped stop reads as exactly at a 5.0 threshold
    rather than clearly past it. With a 6.0 threshold it read as BELOW."""
    volatile = {"ticker": "V", "entry_price": 100.0, "current_stop_price": 95.0,
                "entry_atr_pct": 7.0}
    threshold = 6.0
    assert _position_risk_band_pct(volatile) < threshold    # not counted (wrong)
    assert _position_atr_pct(volatile) >= threshold         # counted (right)


def test_ratcheted_stop_no_longer_shrinks_measured_volatility():
    """A winner's stop ratchets toward the price, so |entry - stop| shrinks and
    the proxy falls over the position's life - the same holding quietly stopped
    counting as high-volatility the better it did. entry_atr_pct is a fact
    about entry and does not move."""
    at_entry = {"ticker": "W", "entry_price": 100.0, "current_stop_price": 93.0,
                "entry_atr_pct": 7.0}
    ratcheted = dict(at_entry, current_stop_price=99.0)
    assert _position_risk_band_pct(ratcheted) < _position_risk_band_pct(at_entry)
    assert _position_atr_pct(ratcheted) == _position_atr_pct(at_entry)


def test_pre_migration_rows_fall_back_to_the_proxy():
    """Rows opened before migrations/011 have no entry_atr_pct and ATR at the
    time is not recoverable. The fallback keeps them counted the old way rather
    than counting them as zero - absent volatility is not zero volatility."""
    pos = {"ticker": "OLD", "entry_price": 100.0, "current_stop_price": 95.0}
    assert _position_atr_pct(pos) == pytest.approx(5.0)


def test_unusable_entry_atr_falls_back_rather_than_raising():
    pos = {"ticker": "BAD", "entry_price": 100.0, "current_stop_price": 95.0,
           "entry_atr_pct": "not a number"}
    assert _position_atr_pct(pos) == pytest.approx(5.0)


def test_high_vol_count_uses_atr_units(db):
    """End to end: a position that is volatile by ATR but tightly stopped must
    count toward max_simultaneous_high_vol_positions. Before §53 it did not."""
    from engine.portfolio_risk import PortfolioRiskEngine

    cfg = {"portfolio_risk": {"enabled": True, "high_vol_atr_pct_threshold": 5.0,
                              "max_simultaneous_high_vol_positions": 1,
                              "min_positions_for_concentration_block": 99,
                              "hard_block_on_severe_breach": False}}
    pid = db.open_position("VOL", 100.0, 1.0, 100.0, simulated=True)
    assert pid is not None
    db.update_position_by_ticker("VOL", {"current_stop_price": 96.0,
                                          "entry_atr_pct": 8.0}, simulated=True)

    res = PortfolioRiskEngine(db).evaluate("NEWV", "Tech", 1.0, 100.0, 9.0, cfg)
    assert res.high_vol_position_count == 1
    assert res.size_multiplier == 0.0    # at the cap, candidate would add another


def test_paper_buy_persists_entry_atr_pct(db):
    from engine import paper_trader
    from tests.test_paper_trading import CFG

    paper_trader.ensure_seeded(db, CFG)
    paper_trader.execute_buy(db, CFG, "ASTS", 20.0,
                             entry_seed={"risk_per_share": 1.0, "entry_atr_pct": 6.25})
    assert db.get_open_position("ASTS", simulated=True)["entry_atr_pct"] == pytest.approx(6.25)


# ── §48: the reset epoch ────────────────────────────────────────────────────

def test_reset_clears_the_equity_curve(db):
    """A reset account inheriting the previous account's curve is how a
    downward re-seed reads as a 33% intraday drawdown (v1.3.1). The epoch guard
    handles a re-seed; a RESET should have nothing to guard against."""
    from engine import paper_trader
    from tests.test_paper_trading import CFG

    paper_trader.ensure_seeded(db, CFG)
    db.record_paper_equity({"total_value": 1000.0, "cash": 1000.0, "n_open": 0})
    assert db.get_paper_equity_history(limit=10)

    db.reset_paper_account()
    assert db.get_paper_equity_history(limit=10) == []
    assert db.get_paper_account() in (None, {})


def test_reset_leaves_the_learning_record_alone(db):
    """The whole argument for the clean slate (§48) is that pattern_database
    survives it, so the closed-outcome history that EV work reads is not the
    thing being discarded."""
    from learning.pattern_database import PatternDatabase

    pdb = PatternDatabase(db)
    pdb.record_entry("KEEP", "SWING", {"_entry_price": 10.0})
    db.reset_paper_account()
    assert len(db.get_patterns(ticker="KEEP", closed_only=False)) == 1


# ── §52: the calibration arithmetic ─────────────────────────────────────────

def test_percentile_returns_an_observation_not_an_interpolation():
    """With a dozen observations, interpolating between two of them invents
    precision the sample does not have. Every value returned is a day that
    actually happened."""
    from scripts.calibrate_risk_caps import _percentile
    s = [0.1, 0.2, 0.5, 1.0, 4.0]
    assert _percentile(s, 100) == 4.0
    assert _percentile(s, 50) in s
    assert _percentile(s, 99) in s
    assert _percentile([], 99) == 0.0


def test_single_point_days_are_skipped_not_scored_zero():
    """One equity point is a level, not a curve. Charging it 0% drawdown drags
    the percentile down with a claim no sample supports."""
    from scripts.calibrate_risk_caps import _intraday_drawdowns
    observed, singles = _intraday_drawdowns({
        "2026-07-20": [1000.0],                      # one point
        "2026-07-21": [1000.0, 980.0, 990.0],        # 2% peak-to-trough
    })
    assert singles == 1
    assert observed == pytest.approx([2.0])


def test_running_drawdown_recovers_where_intraday_does_not():
    """The two caps measure different things and that difference is the reason
    they have different consequences: intraday is a high-water mark for the
    day, running is a current distance that a genuine recovery clears."""
    from scripts.calibrate_risk_caps import _running_drawdowns
    dd = _running_drawdowns([{"total_value": v} for v in (100.0, 80.0, 100.0)])
    assert dd[1] == pytest.approx(20.0)
    assert dd[-1] == pytest.approx(0.0)


# ── §54: the flags stay gone ────────────────────────────────────────────────

@pytest.mark.parametrize("flag", ["daily_loss_limit_triggered",
                                   "daily_profit_lock_triggered"])
def test_writerless_flags_are_not_in_config(flag):
    from config_loader import load_config_dict
    assert flag not in (load_config_dict().get("risk", {}) or {}), (
        f"risk.{flag} is back. Nothing writes it, and "
        f"engine/position_management.py used to read the loss one as a "
        f"priority-1 liquidation - so a hand-edit flattened the book. If a "
        f"manual flatten is wanted, build it with a writer and a test."
    )


@pytest.mark.parametrize("flag", ["daily_loss_limit_triggered",
                                   "daily_profit_lock_triggered"])
def test_no_code_reads_the_removed_flags(flag):
    """A grep test, because the failure mode is a reader surviving the removal
    of its writer - which is the exact shape of the original bug.

    Looks for ACCESSOR syntax specifically, not any mention of the name. The
    names still appear in prose - config.yaml's comment explaining the removal
    and engine/rules_catalog.py's `(REMOVED §54)` entries - and that prose is
    the point: someone who goes looking for the old flag should find out where
    it went rather than finding nothing.
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    # .get("flag"), ["flag"], .get('flag') - how a config value is actually read.
    accessor = re.compile(rf"""(\.get\(\s*["']{re.escape(flag)}["']|\[\s*["']{re.escape(flag)}["']\s*\])""")
    offenders = []
    for path in (list(repo.glob("*.py")) + list(repo.glob("engine/*.py"))
                 + list(repo.glob("rules/*.py")) + list(repo.glob("storage/*.py"))):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if accessor.search(line):
                offenders.append(f"{path.relative_to(repo)}:{i}")
    assert not offenders, f"risk.{flag} is read again at: {offenders}"


def test_kill_switch_still_drives_the_priority_one_exit():
    """Removing the daily-loss branch must not have removed the halt. The
    control that actually fires - trip_kill_switch_if_needed - sets
    kill_switch_triggered, and this is the branch it lands on."""
    from engine.position_management import _evaluate_priority
    out = _evaluate_priority(None, None, None, {"risk": {"kill_switch_triggered": True}})
    assert out["priority"] == 1
    assert out["action"] == "exit_full"
