from scripts.sleeve_vs_spy_report import render, summarize


def test_summarize_months_total_and_drawdown():
    rows = [{"as_of_date": "2026-01-02", "total_value": 100.0, "spy_close": 100.0},
            {"as_of_date": "2026-01-30", "total_value": 102.0, "spy_close": 105.0},
            {"as_of_date": "2026-02-27", "total_value": 101.0, "spy_close": 95.0},
            {"as_of_date": "2026-03-31", "total_value": 104.0, "spy_close": 100.0}]
    r = summarize(rows)
    assert abs(r["sleeve_total"] - 0.04) < 1e-9 and abs(r["spy_total"]) < 1e-9
    assert r["sleeve_dd"] > -0.02 and r["spy_dd"] < -0.09
    txt = render(r)
    assert "2026-02" in txt and "Only 3 month" in txt
