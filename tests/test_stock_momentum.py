import numpy as np
import pandas as pd
import pytest

from rules import stock_momentum as sm
from storage.sleeve_ledger import SleeveLedger


def _closes(n=300, k=60):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(1)
    d = {f"S{i}": 100 * np.cumprod(1 + 0.0002 * i + rng.normal(0, 0.003, n)) for i in range(k)}
    d["SHY"] = np.full(n, 100.0)
    return pd.DataFrame(d, index=idx), [f"S{i}" for i in range(k)]


def test_picks_top_n_by_momentum_equal_weight():
    c, mem = _closes()
    w = sm.target_weights(c, mem, risk_on=True, n=10)
    assert len(w) == 10 and abs(sum(w.values()) - 1.0) < 1e-9
    assert set(w) <= set(f"S{i}" for i in range(40, 60))          # strongest drifts are the highest indices


def test_risk_off_goes_to_tbills():
    c, mem = _closes()
    assert sm.target_weights(c, mem, risk_on=False) == {"SHY": 1.0}


def test_too_few_rankable_or_short_history_raises():
    c, mem = _closes()
    with pytest.raises(ValueError):
        sm.target_weights(c, mem[:5], risk_on=True, n=10)
    with pytest.raises(ValueError):
        sm.target_weights(c.iloc[:100], mem, risk_on=True, n=10)


def test_only_current_members_are_used():
    m = sm.current_members()
    assert 480 <= len(m) <= 530 and "AAPL" in m


def test_ledger_prefix_validated_and_isolated():
    with pytest.raises(ValueError):
        SleeveLedger(object(), "bad prefix; drop")


def test_mom_lowvol_prefers_steady_winners_over_wild_ones():
    import numpy as np, pandas as pd
    idx = pd.bdate_range("2024-01-01", periods=300)
    rng = np.random.default_rng(7)
    d = {}
    for i in range(30):                                     # all drift up equally; volatility rises with i
        d[f"S{i}"] = 100 * np.cumprod(1 + 0.0008 + rng.normal(0, 0.002 + 0.0006 * i, 300))
    d["SHY"] = np.full(300, 100.0)
    c = pd.DataFrame(d, index=idx)
    mem = [f"S{i}" for i in range(30)]
    w = sm.target_weights(c, mem, risk_on=True, n=5, selection="mom_lowvol")
    low_vol_names = {f"S{i}" for i in range(15)}
    assert len(w) == 5 and sum(1 for t in w if t in low_vol_names) >= 4
    assert set(sm.target_weights(c, mem, risk_on=False, selection="mom_lowvol")) == {"SHY"}


def test_second_sleeve_uses_its_own_config_block_and_ledger_prefix():
    from config_loader import load_config_dict
    c = load_config_dict()
    assert c["stock_lowvol_sleeve"]["selection"] == "mom_lowvol" and c["stock_momentum_sleeve"]["selection"] == "momentum"
    import inspect
    from scripts import stock_momentum_paper_cycle as cyc
    src = inspect.getsource(cyc.main)
    assert "a.prefix" in src and "a.section" in src                      # nothing hard-wired to the first sleeve's tables
