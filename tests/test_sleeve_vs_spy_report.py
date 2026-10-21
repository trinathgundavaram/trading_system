from scripts.sleeve_vs_spy_report import render, summarize

ROWS = [{"as_of_date": "2026-01-02", "total_value": 100.0, "spy_close": 100.0},
        {"as_of_date": "2026-01-30", "total_value": 102.0, "spy_close": 105.0},
        {"as_of_date": "2026-02-27", "total_value": 101.0, "spy_close": 95.0},
        {"as_of_date": "2026-03-31", "total_value": 104.0, "spy_close": 100.0}]


def test_summarize_months_total_and_drawdown():
    r = summarize(ROWS)
    assert abs(r["sleeve_total"] - 0.04) < 1e-9 and abs(r["spy_total"]) < 1e-9
    assert r["sleeve_dd"] > -0.02 and r["spy_dd"] < -0.09
    txt = render(r)
    assert "2026-02" in txt and "Only 3 month" in txt and "Swing" not in txt


def test_swing_column_uses_last_value_on_or_before_each_date():
    swing = [{"timestamp": "2025-12-15T10:00:00", "total_value": 1000.0},
             {"timestamp": "2026-02-10T10:00:00", "total_value": 990.0},
             {"timestamp": "2026-03-20T10:00:00", "total_value": 1010.0}]
    r = summarize(ROWS, swing)
    assert abs(r["swing_total"] - 0.01) < 1e-9          # 1000 (carried) -> 1010
    assert "Swing engine" in render(r)
