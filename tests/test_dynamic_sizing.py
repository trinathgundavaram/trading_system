"""Test dynamic position sizing (2026-07-27).

Tests that position size scales with portfolio size when use_dynamic_sizing
is enabled, and falls back to static sizing when disabled.
"""
import pytest


def test_dynamic_sizing_calculation():
    """Position size should scale with portfolio when enabled."""
    from engine.position_sizing import _get_portfolio_total

    # Mock a portfolio with some positions
    class MockDB:
        def get_all_positions(self, simulated=True):
            return [
                {"ticker": "A", "dollar_amount": 100.0, "closed_at": None},
                {"ticker": "B", "dollar_amount": 150.0, "closed_at": None},
                {"ticker": "C", "dollar_amount": 50.0, "closed_at": None},
            ]

    db = MockDB()
    total = _get_portfolio_total(db, simulated=True)

    assert total == 300.0, f"Expected $300, got ${total}"


def test_dynamic_sizing_ignores_closed_positions():
    """Portfolio total should exclude closed positions."""
    from engine.position_sizing import _get_portfolio_total

    class MockDB:
        def get_all_positions(self, simulated=True):
            return [
                {"ticker": "A", "dollar_amount": 100.0, "closed_at": None},
                {"ticker": "B", "dollar_amount": 150.0, "closed_at": "2026-07-27 10:00:00"},  # closed
                {"ticker": "C", "dollar_amount": 50.0, "closed_at": None},
            ]

    db = MockDB()
    total = _get_portfolio_total(db, simulated=True)

    assert total == 150.0, f"Should exclude closed positions, got ${total}"


def test_dynamic_sizing_empty_portfolio():
    """Empty portfolio should return 0."""
    from engine.position_sizing import _get_portfolio_total

    class MockDB:
        def get_all_positions(self, simulated=True):
            return []

    db = MockDB()
    total = _get_portfolio_total(db, simulated=True)

    assert total == 0.0, f"Expected $0 for empty portfolio, got ${total}"


def test_position_size_scales_with_portfolio():
    """Position size calculation should scale with portfolio size."""
    from engine.position_sizing import calculate

    class MockDB:
        def __init__(self, portfolio_total):
            self.portfolio_total = portfolio_total

        def get_all_positions(self, simulated=True):
            # Mock positions totaling self.portfolio_total
            if self.portfolio_total > 0:
                return [
                    {"ticker": "EXISTING", "dollar_amount": self.portfolio_total, "closed_at": None}
                ]
            return []

    # Mock buy result
    class MockBuyResult:
        should_buy = True
        pct_score = 75.0

    # Mock score result
    class MockScoreResult:
        pass

    # Config with dynamic sizing enabled
    cfg = {
        "trading": {
            "use_dynamic_sizing": True,
            "position_size_pct_of_portfolio": 5.0,  # 5% per trade
            "trade_size_usd": 100,  # fallback
            "watch_execute": "WATCH",
        },
        "risk": {"max_position_size_usd": 1000.0},
        "position_sizing": {"enabled": True, "score_tiers": [{"min_score": 0, "size_pct": 100}]},
    }

    # Small portfolio: $100
    db_small = MockDB(100.0)
    result_small = calculate(
        MockBuyResult(),
        MockScoreResult(),
        {"ticker": "TEST", "atr": 1.0, "price": 50.0},
        regime=None,
        cfg=cfg,
        db=db_small,
    )

    # Large portfolio: $1000 (10x larger)
    db_large = MockDB(1000.0)
    result_large = calculate(
        MockBuyResult(),
        MockScoreResult(),
        {"ticker": "TEST", "atr": 1.0, "price": 50.0},
        regime=None,
        cfg=cfg,
        db=db_large,
    )

    # Large portfolio position should be ~10x larger than small
    size_ratio = result_large.suggested_dollar_amount / result_small.suggested_dollar_amount

    # Allow some tolerance due to other multipliers, but should be roughly 10x
    assert 5.0 < size_ratio < 15.0, (
        f"Position size not scaling with portfolio. "
        f"$100 portfolio: ${result_small.suggested_dollar_amount:.2f}, "
        f"$1000 portfolio: ${result_large.suggested_dollar_amount:.2f}, "
        f"ratio: {size_ratio:.2f}x (expected ~10x)"
    )


def test_static_sizing_when_disabled():
    """When dynamic sizing disabled, should use static trade_size_usd."""
    from engine.position_sizing import calculate

    class MockDB:
        def get_all_positions(self, simulated=True):
            return [{"ticker": "EXISTING", "dollar_amount": 1000.0, "closed_at": None}]

    class MockBuyResult:
        should_buy = True
        pct_score = 75.0

    class MockScoreResult:
        pass

    # Config with dynamic sizing DISABLED
    cfg = {
        "trading": {
            "use_dynamic_sizing": False,  # ← Disabled
            "position_size_pct_of_portfolio": 5.0,
            "trade_size_usd": 100,  # Use this instead
            "watch_execute": "WATCH",
        },
        "risk": {"max_position_size_usd": 1000.0},
        "position_sizing": {"enabled": True, "score_tiers": [{"min_score": 0, "size_pct": 100}]},
    }

    db = MockDB()
    result = calculate(
        MockBuyResult(),
        MockScoreResult(),
        {"ticker": "TEST", "atr": 1.0, "price": 50.0},
        regime=None,
        cfg=cfg,
        db=db,
    )

    # Should use static $100 as base allocation
    assert result.base_allocation_usd == 100.0, (
        f"Static sizing should use trade_size_usd, "
        f"got base_allocation_usd={result.base_allocation_usd}"
    )


def test_fallback_to_static_on_empty_portfolio():
    """First trade (empty portfolio) should fall back to static sizing."""
    from engine.position_sizing import calculate

    class MockDB:
        def get_all_positions(self, simulated=True):
            return []  # Empty portfolio

    class MockBuyResult:
        should_buy = True
        pct_score = 75.0

    class MockScoreResult:
        pass

    cfg = {
        "trading": {
            "use_dynamic_sizing": True,  # Enabled
            "position_size_pct_of_portfolio": 3.0,
            "trade_size_usd": 100,  # Fallback
            "watch_execute": "WATCH",
        },
        "risk": {"max_position_size_usd": 1000.0},
        "position_sizing": {"enabled": True, "score_tiers": [{"min_score": 0, "size_pct": 100}]},
    }

    db = MockDB()
    result = calculate(
        MockBuyResult(),
        MockScoreResult(),
        {"ticker": "TEST", "atr": 1.0, "price": 50.0},
        regime=None,
        cfg=cfg,
        db=db,
    )

    # Should fall back to $100 on empty portfolio
    assert result.base_allocation_usd == 100.0, (
        f"Empty portfolio should fall back to trade_size_usd, "
        f"got base_allocation_usd={result.base_allocation_usd}"
    )
