import pandas as pd

from engine import tradingagents_advisor as adv
from scripts.tradingagents_report import forward_excess


def test_parse_rating_prefers_specific_and_handles_unknown():
    assert adv.parse_rating("Final decision: STRONG BUY") == "STRONG BUY"
    assert adv.parse_rating("rating: Underweight, reduce") == "UNDERWEIGHT"
    assert adv.parse_rating("no view") == "UNKNOWN"
    assert adv.bucket("OVERWEIGHT") == "approve" and adv.bucket("SELL") == "veto" and adv.bucket("HOLD") == "neutral"


def test_forward_excess_groups_and_skips_unmatured():
    idx = pd.bdate_range("2026-01-01", periods=60)
    closes = pd.DataFrame({"SPY": 100.0, "AAA": [100 + i for i in range(60)]}, index=idx)
    rows = [{"as_of_date": "2026-01-05", "ticker": "AAA", "rating": "BUY"},
            {"as_of_date": "2026-03-20", "ticker": "AAA", "rating": "SELL"}]      # too recent
    res = forward_excess(rows, 20, closes)
    assert len(res["approve"]) == 1 and res["approve"][0] > 0 and "veto" not in res


def test_shortlist_caps_and_excludes_bonds():
    class Db:
        def get_open_sm_positions(self): return [{"ticker": t} for t in ("XLK", "IEF", "XLE")]
    cfg = {"tradingagents_advisor": {"max_tickers_per_day": 2, "extra_tickers": ["NVDA"]}}
    assert adv.shortlist(cfg, Db()) == ["XLK", "XLE"]
