"""scripts/equity_curve.py (2026-07-28) - the compounded equity-curve/Sharpe
simulator built after Trinath asked "what is the return percent per year
based on all this validation," then extended the same day after: "do not
test for unlimited cash... go for a fixed start capital and continue with
that portfolio." This pins both the original compounding/Sharpe math AND the
capital-constraint model (a position whose dollar VALUE - not just its
dollar RISK - exceeds available cash at open must be REJECTED, not resized),
entirely with synthetic trades (no DB/network)."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_spec = importlib.util.spec_from_file_location(
    "equity_curve", os.path.join(os.path.dirname(__file__), "..", "scripts", "equity_curve.py"))
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

from datetime import date  # noqa: E402


def _trade(entry, exit_, outcome_r, risk_pct=5.0):
    # risk_pct=5.0 default: risking 1% of equity at a 5%-away stop deploys
    # 1%/5% = 20% of equity as position VALUE - comfortably affordable
    # against a $100k start in every test below unless a test deliberately
    # sizes it to exceed available cash.
    return {"entry_date": entry, "exit_date": exit_, "outcome_r": outcome_r, "risk_pct": risk_pct}


# --- simulate: basic compounding -------------------------------------------

def test_single_winning_trade_compounds_correctly():
    trades = [_trade("2024-01-01", "2024-01-05", 2.0)]  # +2R
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    # risked = 1% of 100k = 1000; +2R = +2000 -> 102000
    assert r["final_equity"] == 102_000.0
    assert r["n_trades_simulated"] == 1


def test_single_losing_trade():
    trades = [_trade("2024-01-01", "2024-01-05", -1.0)]  # full stop-out, -1R
    r = ec.simulate(trades, risk_pct=2.0, starting_equity=100_000.0)
    assert r["final_equity"] == 98_000.0


def test_sequential_trades_compound_off_updated_equity():
    """Second trade must size off the equity AFTER the first trade closed,
    not off starting_equity - that's the whole point of compounding."""
    trades = [
        _trade("2024-01-01", "2024-01-05", 1.0),   # 100k -> 101k (1% risk)
        _trade("2024-01-06", "2024-01-10", 1.0),   # risk 1% of 101k = 1010 -> 102010
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["final_equity"] == 102_010.0


def test_overlapping_trades_both_size_off_the_same_pre_close_equity():
    """Two trades open at once, closing later - both should have sized their
    risk off the equity level AT THEIR OWN OPEN, which for two trades opened
    before either closes is the same starting figure (documented
    "no capital ceiling enforced" simplification - see module docstring)."""
    trades = [
        _trade("2024-01-01", "2024-01-10", 1.0),
        _trade("2024-01-02", "2024-01-11", 1.0),
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    # both risk 1% of 100k = 1000 each; first closes 100k->101k, second still
    # risked 1000 (fixed at ITS open, before either had closed) -> 101k+1000
    assert r["final_equity"] == 102_000.0


def test_trades_without_outcome_r_are_skipped_not_crashed():
    trades = [
        _trade("2024-01-01", "2024-01-05", None),
        _trade("2024-01-06", "2024-01-10", 1.0),
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["n_trades_simulated"] == 1
    assert r["skipped_no_risk_basis"] == 1


def test_no_trades_with_risk_basis_returns_zero_simulated():
    trades = [_trade("2024-01-01", "2024-01-05", None)]
    r = ec.simulate(trades, risk_pct=1.0)
    assert r["n_trades_simulated"] == 0


# --- max drawdown ------------------------------------------------------------

def test_max_drawdown_tracks_peak_to_trough():
    trades = [
        _trade("2024-01-01", "2024-01-02", 2.0),    # up
        _trade("2024-01-03", "2024-01-04", -1.0),   # down from peak
        _trade("2024-01-05", "2024-01-06", -1.0),   # further down
        _trade("2024-01-07", "2024-01-08", 5.0),    # recovers, new peak
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["max_drawdown_pct"] > 0
    # peak after trade 1: 102000. trough after trade 3: 102000*(1-0.01)*(1-0.01)... roughly a ~2% dd
    assert 1.5 < r["max_drawdown_pct"] < 2.5


# --- CAGR sanity -------------------------------------------------------------

def test_cagr_matches_total_return_for_a_one_year_window():
    trades = [_trade("2024-01-01", "2024-12-31", 10.0)]  # big single winner, exactly 1 year span
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert abs(r["years"] - 1.0) < 0.01
    assert abs(r["cagr_pct"] - r["total_return_pct"]) < 0.5  # ~equal for a 1-year window


# --- _month_ends --------------------------------------------------------------

def test_month_ends_spans_inclusive_range():
    ends = ec._month_ends(date(2024, 1, 15), date(2024, 3, 5))
    assert ends == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]


# --- _sharpe ------------------------------------------------------------------

def test_sharpe_none_with_too_few_months():
    curve = [("2024-01-05", 101_000.0)]
    info = ec._sharpe(curve, date(2024, 1, 1), date(2024, 1, 10), 100_000.0, 0.0)
    assert info["sharpe"] is None


def test_sharpe_is_positive_for_a_steadily_rising_curve():
    curve = [(f"2024-{m:02d}-15", 100_000.0 * (1.01 ** m)) for m in range(1, 8)]
    info = ec._sharpe(curve, date(2024, 1, 1), date(2024, 7, 31), 100_000.0, 0.0)
    assert info["sharpe"] is not None
    assert info["sharpe"] > 0


def test_sharpe_penalizes_volatility_at_the_same_mean_return():
    """Two curves with the same total return but one choppier - the choppier
    one must score a lower Sharpe."""
    smooth = [(f"2024-{m:02d}-15", 100_000.0 * (1.02 ** m)) for m in range(1, 7)]
    choppy_vals = [100_000, 106_000, 98_000, 112_000, 101_000, 118_000]
    choppy = [(f"2024-{m:02d}-15", v) for m, v in zip(range(1, 7), choppy_vals)]
    s_smooth = ec._sharpe(smooth, date(2024, 1, 1), date(2024, 6, 30), 100_000.0, 0.0)["sharpe"]
    s_choppy = ec._sharpe(choppy, date(2024, 1, 1), date(2024, 6, 30), 100_000.0, 0.0)["sharpe"]
    assert s_smooth > s_choppy


# --- fixed-capital constraint (2026-07-28 afternoon: "do not test for
# unlimited cash... go for a fixed start capital and continue with that
# portfolio" - a position's dollar VALUE, not just its dollar RISK, must fit
# within available cash or the trade is rejected outright) -----------------

def test_trade_missing_risk_pct_is_skipped_like_missing_outcome_r():
    trades = [{"entry_date": "2024-01-01", "exit_date": "2024-01-05", "outcome_r": 1.0}]  # no risk_pct
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["n_trades_simulated"] == 0
    assert r["skipped_no_risk_basis"] == 1


def test_a_single_trade_that_needs_more_capital_than_exists_is_rejected():
    """risk_pct(trade)=1% stop -> position_value = dollar_risk / 0.01 = 100x
    the risked amount. Risking 2% of a $100k account needs $200k of capital
    to buy - more than the account has - so this trade must be rejected
    entirely, not opened at a smaller size."""
    trades = [_trade("2024-01-01", "2024-01-05", 5.0, risk_pct=1.0)]  # tight 1% stop
    r = ec.simulate(trades, risk_pct=2.0, starting_equity=100_000.0)
    assert r["n_trades_simulated"] == 0
    assert r["n_capital_constrained_rejected"] == 1
    assert r["final_equity"] if "final_equity" in r else True  # no crash either way


def test_second_overlapping_trade_rejected_when_cash_runs_out():
    """Trade A deploys most of the account's cash and is still open when
    Trade B tries to open - B must be rejected, and its later close must
    not be double-counted or crash."""
    trades = [
        _trade("2024-01-01", "2024-01-10", 1.0, risk_pct=5.0),   # pv = (1%*100k)/5% = 20000, fine
        _trade("2024-01-02", "2024-01-11", 1.0, risk_pct=0.5),   # pv = (1%*100k)/0.5% = 200000, way too big
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["n_trades_simulated"] == 1
    assert r["n_capital_constrained_rejected"] == 1
    # only trade A's +1R on its 1000 risked dollars should have landed
    assert r["final_equity"] == 101_000.0


def test_unlimited_capital_flag_reproduces_old_always_open_behaviour():
    """The exact scenario above, but with --unlimited-capital: both trades
    open regardless of cash, matching the pre-2026-07-28-afternoon model."""
    trades = [
        _trade("2024-01-01", "2024-01-10", 1.0, risk_pct=5.0),
        _trade("2024-01-02", "2024-01-11", 1.0, risk_pct=0.5),
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, unlimited_capital=True)
    assert r["n_trades_simulated"] == 2
    assert r["n_capital_constrained_rejected"] == 0
    # both trades size off the equity level at THEIR OWN open (both still
    # 100k when B opens, since A hasn't closed yet) -> both risk 1000,
    # +1R each -> 100000 + 1000 + 1000 = 102000
    assert r["final_equity"] == 102_000.0


# --- sizing_mode: the sequence-of-returns fix (2026-07-28 evening, sec 9e) -
#
# docs/beating_the_market_analysis_2026-07-28.md sec 9d's blind walk-forward
# found PF=1.17 (trade-level profitable) alongside CAGR=-5.1% (compounded
# loss) on the SAME trade sequence - risking a fixed %% of CURRENT equity
# means a losing stretch shrinks every later position, so later wins recover
# fewer dollars than earlier losses cost even when the later trades are
# individually just as good. high_watermark sizing (risk %% of the highest
# equity ever reached, not current equity) is the candidate fix - these
# tests pin its exact behavior with a small, hand-verifiable scenario that
# reproduces the same PF-positive/CAGR-negative shape in miniature.

def test_high_watermark_sizing_matches_current_equity_absent_any_drawdown():
    """With no drawdown ever (equity only goes up), peak == equity at every
    open - the two sizing modes MUST be identical. This is the check that
    high_watermark isn't just "a different number", it's specifically
    "the same as current_equity except during a drawdown"."""
    trades = [
        _trade("2024-01-01", "2024-01-05", 1.0, risk_pct=5.0),
        _trade("2024-01-06", "2024-01-10", 2.0, risk_pct=5.0),
        _trade("2024-01-11", "2024-01-15", 0.5, risk_pct=5.0),
    ]
    r_cur = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, sizing_mode="current_equity")
    r_hwm = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, sizing_mode="high_watermark")
    assert r_cur["final_equity"] == r_hwm["final_equity"]


def test_high_watermark_sizing_recovers_a_loss_current_equity_sizing_cannot():
    """Hand-verifiable two-trade scenario: a full stop-out (-1.0R) followed
    by a winner (+1.05R) - total R is positive (+0.05), i.e. "PF profitable"
    in aggregate, exactly sec 9d's shape. Under current_equity sizing the
    second trade sizes off the SHRUNK post-loss equity (90,000, not 100,000)
    and the account ends net negative (99,450) despite positive total R.
    Under high_watermark sizing the second trade sizes off the ORIGINAL
    peak (100,000, unchanged by the still-open drawdown) and the account
    recovers to a net gain (100,500) on the IDENTICAL trade sequence."""
    trades = [
        _trade("2024-01-01", "2024-01-02", -1.0, risk_pct=20.0),   # full stop-out
        _trade("2024-01-03", "2024-01-04", 1.05, risk_pct=20.0),   # recovers more than 1R
    ]
    r_cur = ec.simulate(trades, risk_pct=10.0, starting_equity=100_000.0, sizing_mode="current_equity")
    r_hwm = ec.simulate(trades, risk_pct=10.0, starting_equity=100_000.0, sizing_mode="high_watermark")

    assert abs(r_cur["final_equity"] - 99_450.0) < 1e-6, r_cur
    assert r_cur["final_equity"] < 100_000.0, "current_equity mode should show a net LOSS here"

    assert abs(r_hwm["final_equity"] - 100_500.0) < 1e-6, r_hwm
    assert r_hwm["final_equity"] > 100_000.0, "high_watermark mode should show a net GAIN on the same trades"

    assert r_hwm["final_equity"] > r_cur["final_equity"], (
        "high_watermark sizing should outperform current_equity sizing on this exact "
        "sequence-of-returns scenario - if this fails, the fix isn't doing what it's for")


def test_sizing_mode_is_reported_on_the_result():
    trades = [_trade("2024-01-01", "2024-01-05", 1.0)]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["sizing_mode"] == "current_equity"
    r2 = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, sizing_mode="high_watermark")
    assert r2["sizing_mode"] == "high_watermark"


def test_invalid_sizing_mode_raises():
    trades = [_trade("2024-01-01", "2024-01-05", 1.0)]
    try:
        ec.simulate(trades, risk_pct=1.0, sizing_mode="not_a_real_mode")
        assert False, "expected a ValueError for an invalid sizing_mode"
    except ValueError:
        pass


# --- capital_mode: partial_resize (2026-07-31, sec 9e follow-up) - "next
# step would be a partial-resize capital model instead of hard rejection".
# The real validation run found high_watermark sizing UNDERPERFORMING
# current_equity under the "reject" capital rule (1.3% vs 9.9% CAGR) despite
# clearly WINNING under --unlimited-capital (24.0% vs 20.5%) - i.e. "reject"
# was itself defeating the mechanism it was supposed to let play out. These
# tests pin partial_resize's exact behavior with a small, hand-verifiable
# scenario reproducing that shape in miniature.

def test_capital_mode_defaults_to_reject_and_is_reported():
    trades = [_trade("2024-01-01", "2024-01-05", 1.0)]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["capital_mode"] == "reject"


def test_invalid_capital_mode_raises():
    trades = [_trade("2024-01-01", "2024-01-05", 1.0)]
    try:
        ec.simulate(trades, risk_pct=1.0, capital_mode="not_a_real_mode")
        assert False, "expected a ValueError for an invalid capital_mode"
    except ValueError:
        pass


def test_partial_resize_matches_reject_when_everything_fits_normally():
    """No capital shortfall ever occurs - the two capital_modes must be
    identical (partial_resize is a no-op absent a shortfall, same spirit as
    the high_watermark-matches-current_equity-absent-a-drawdown test)."""
    trades = [
        _trade("2024-01-01", "2024-01-05", 1.0, risk_pct=20.0),
        _trade("2024-01-06", "2024-01-10", 2.0, risk_pct=20.0),
    ]
    r_reject = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, capital_mode="reject")
    r_resize = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0, capital_mode="partial_resize")
    assert r_reject["final_equity"] == r_resize["final_equity"]
    assert r_resize["n_resized"] == 0


def test_partial_resize_opens_smaller_instead_of_skipping_and_beats_reject_under_high_watermark():
    """The scenario sec 9e's real run actually hit: a big loss leaves less
    cash than high_watermark sizing (still pegged to the UN-shrunk peak)
    wants for the recovery trade. Hand-worked exactly:

    Trade 1 (risk_pct=100 -> position_value == dollar_risk): sizing_base
    starts at peak=equity=100,000; risked = 60%%*100,000 = 60,000 = pv.
    Opens (fits in 100,000 cash), cash -> 40,000. Full stop-out (-1.0R):
    pnl=-60,000, equity -> 40,000, cash -> 40,000 (pv+pnl nets to 0 back
    into cash), peak stays 100,000 (only ever moves up on a close, and this
    close is a loss).

    Trade 2 (risk_pct=100): high_watermark sizing_base = peak = 100,000
    (UNCHANGED by trade 1's loss - the whole point of this sizing mode) ->
    desired risked=pv=60,000, but only 40,000 cash remains.
      - reject: trade 2 is skipped entirely -> final_equity stays 40,000,
        the account never gets to attempt a recovery at all.
      - partial_resize: trade 2 opens at pv=40,000 (all available cash),
        risked scaled down to match (40,000*100/100=40,000). A +1.05R
        recovery -> pnl=42,000 -> final_equity = 40,000+42,000 = 82,000 -
        NOT a full recovery to 100,000 (real capital constraint still
        bound, it wasn't waived), but dramatically better than reject's
        40,000, and it demonstrates the exact mechanism the fix is for."""
    trades = [
        _trade("2024-01-01", "2024-01-02", -1.0, risk_pct=100.0),
        _trade("2024-01-03", "2024-01-04", 1.05, risk_pct=100.0),
    ]
    r_reject = ec.simulate(trades, risk_pct=60.0, starting_equity=100_000.0,
                            sizing_mode="high_watermark", capital_mode="reject")
    r_resize = ec.simulate(trades, risk_pct=60.0, starting_equity=100_000.0,
                            sizing_mode="high_watermark", capital_mode="partial_resize")

    assert r_reject["n_trades_simulated"] == 1
    assert r_reject["n_capital_constrained_rejected"] == 1
    assert abs(r_reject["final_equity"] - 40_000.0) < 1e-6, r_reject

    assert r_resize["n_trades_simulated"] == 2
    assert r_resize["n_capital_constrained_rejected"] == 0
    assert r_resize["n_resized"] == 1
    assert abs(r_resize["smallest_resized_position_value"] - 40_000.0) < 1e-6, r_resize
    assert abs(r_resize["final_equity"] - 82_000.0) < 1e-6, r_resize

    assert r_resize["final_equity"] > r_reject["final_equity"], (
        "partial_resize should let high_watermark sizing recover more than reject does on "
        "this exact sequence-of-returns-plus-capital-constraint scenario - if this fails, "
        "the fix isn't doing what sec 9e built it for")


def test_partial_resize_still_rejects_when_cash_is_fully_exhausted():
    """Two trades opening while overlapping (B opens before A closes) -
    A consumes ALL cash, leaving exactly 0 for B. partial_resize must NOT
    open a $0 position - that's not "smaller", it's meaningless - so this
    must still count as a genuine rejection."""
    trades = [
        _trade("2024-01-01", "2024-01-10", 1.0, risk_pct=100.0),   # pv = 100%*100k/100% = 100000, uses ALL cash
        _trade("2024-01-02", "2024-01-11", 1.0, risk_pct=100.0),   # opens while A still open - 0 cash left
    ]
    r = ec.simulate(trades, risk_pct=100.0, starting_equity=100_000.0, capital_mode="partial_resize")
    assert r["n_trades_simulated"] == 1
    assert r["n_capital_constrained_rejected"] == 1
    assert r["n_resized"] == 0


def test_capital_freed_on_close_allows_a_later_trade_to_open():
    """Trade A ties up most of the cash, closes, and only THEN does Trade B
    (which needs more than was available while A was still open) get its
    chance - sequential, not overlapping, so it must succeed."""
    trades = [
        _trade("2024-01-01", "2024-01-05", 1.0, risk_pct=1.0),   # pv = 1000/0.01 = 100000 (nearly all cash)
        _trade("2024-01-06", "2024-01-10", 1.0, risk_pct=1.0),   # opens AFTER A already closed and freed cash
    ]
    r = ec.simulate(trades, risk_pct=1.0, starting_equity=100_000.0)
    assert r["n_trades_simulated"] == 2
    assert r["n_capital_constrained_rejected"] == 0
