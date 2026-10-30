"""The honest yardstick for the sleeves: SPY held at the SAME leverage under the SAME 200-day rule.

If a sleeve cannot beat this after costs, its stock/sector selection adds nothing over simply levering the index
with the market-timing rule. Month-end decisions, same hysteresis band, financing charged on borrowed money, nothing
earned while out of the market (conservative). Needs only SPY closes, which the sleeves already log daily."""
from __future__ import annotations

import json

import pandas as pd

STATE_KEY = "baseline_curve"


def curve(spy: pd.Series, leverage: float, band: float = 0.0, financing_bps: float = 400.0) -> pd.Series:
    """spy: daily closes (oldest first, >= 200 rows before the window of interest). Returns equity indexed to 1.0
    at the first row. Exposure is decided on month-end closes and applied from the next day."""
    spy = spy.dropna().astype(float)
    sma = spy.rolling(200).mean()
    ret = spy.pct_change().fillna(0.0)
    month_end = {d for d in spy.index if (d + pd.offsets.BDay(1)).month != d.month}
    on, eq, out = None, 1.0, []
    fin = financing_bps / 1e4 / 252
    for i, d in enumerate(spy.index):
        expo = (leverage if on else 0.0) if on is not None else 0.0
        eq *= 1.0 + expo * ret.iloc[i] - max(expo - 1.0, 0.0) * fin
        out.append(eq)
        if d in month_end and pd.notna(sma.iloc[i]):
            hi, lo = sma.iloc[i] * (1 + band), sma.iloc[i] * (1 - band)
            if spy.iloc[i] >= hi:
                on = True
            elif spy.iloc[i] < lo:
                on = False
            elif on is None:
                on = bool(spy.iloc[i] >= sma.iloc[i])
    return pd.Series(out, index=spy.index)


def refresh(db, cfg: dict) -> int:
    """Recompute and store the baseline curve for the sector sleeve's window (free Yahoo data, once a day)."""
    import yfinance as yf
    sc = (cfg or {}).get("sector_momentum_sleeve", {}) or {}
    from storage.sleeve_ledger import SleeveLedger
    marks = SleeveLedger(db, "sm").get_sm_equity_log()
    if not marks:
        return 0
    start = min(r["as_of_date"] for r in marks)
    spy = yf.download("SPY", period="2y", auto_adjust=True, progress=False)["Close"]
    spy = spy.squeeze()
    c = curve(spy, float(sc.get("leverage", 1.0)), float(sc.get("band_pct", 0.0)) / 100.0, float(sc.get("financing_bps", 400.0)))
    c = c[c.index >= pd.Timestamp(start)]
    c = c / c.iloc[0]
    db.set_sm_state(STATE_KEY, json.dumps({str(d.date()): round(float(v), 6) for d, v in c.items()}))
    return len(c)


def load(db) -> dict:
    try:
        return json.loads(db.get_sm_state(STATE_KEY) or "{}")
    except Exception:
        return {}
