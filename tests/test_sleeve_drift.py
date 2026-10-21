from datetime import date

import pandas as pd

from engine import sleeve_drift as sd

CFG = {"sector_momentum_sleeve": {"leverage": 2.0, "band_pct": 2.0}}
META = {"leverage": "2.0", "band": "2.0"}


def _ref(n=800):
    # alternating +1% / -0.4% days: positive drift with real dips
    return META, pd.Series([0.01 if i % 2 == 0 else -0.004 for i in range(n)],
                           index=pd.bdate_range("2010-01-01", periods=n))


def _log(values, end=date(2026, 10, 20)):
    dates = pd.bdate_range(end=end, periods=len(values))
    return [{"as_of_date": d.strftime("%Y-%m-%d"), "total_value": v} for d, v in zip(dates, values)]


def _grow(n, daily):
    v, out = 10000.0, []
    for _ in range(n + 1):
        out.append(v); v *= 1 + daily
    return out


ACC = {"gross": 2.0}


def test_collecting_until_enough_marks():
    r = sd.evaluate(_log(_grow(5, 0.001)), ACC, CFG, ref=_ref(), today=date(2026, 10, 20))
    assert r["status"] == "collecting" and r["n_days"] == 5


def test_typical_performance_is_ok():
    r = sd.evaluate(_log(_grow(30, 0.003)), ACC, CFG, ref=_ref(), today=date(2026, 10, 20))
    assert r["status"] == "ok", r["checks"]


def test_far_below_history_alerts():
    r = sd.evaluate(_log(_grow(30, -0.02)), ACC, CFG, ref=_ref(), today=date(2026, 10, 20))
    assert r["status"] == "alert"
    assert {c["name"] for c in r["checks"] if c["status"] == "alert"} >= {"Return", "Drawdown"}


def test_reference_mismatch_watches():
    r = sd.evaluate(_log(_grow(30, 0.003)), {"gross": 1.25}, {"sector_momentum_sleeve": {"leverage": 1.25, "band_pct": 2.0}},
                    ref=_ref(), today=date(2026, 10, 20))
    assert r["status"] == "watch" and r["checks"][0]["name"] == "Reference"


def test_exposure_and_staleness():
    r = sd.evaluate(_log(_grow(30, 0.003)), {"gross": 2.4}, CFG, ref=_ref(), today=date(2026, 10, 20))
    assert any(c["name"] == "Exposure" and c["status"] == "alert" for c in r["checks"])
    stale = sd.evaluate(_log(_grow(30, 0.003), end=date(2026, 10, 9)), ACC, CFG, ref=_ref(), today=date(2026, 10, 20))
    assert any(c["name"] == "Freshness" and c["status"] == "alert" for c in stale["checks"])


def test_real_reference_file_loads_and_matches_config():
    meta, ser = sd.load_reference()
    assert meta is not None and len(ser) > 4000 and meta["leverage"] == "2.0" and meta["band"] == "2.0"
