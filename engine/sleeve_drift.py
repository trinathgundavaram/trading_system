"""Live-vs-backtest drift monitor for the sector-momentum paper sleeve.

Question it answers: is the live paper sleeve behaving like something drawn from
the backtest, or has it drifted outside what history says is normal? It does
NOT predict returns and it does not change any trade - it only reports.

Reference: research/reference/sleeve_ref_returns.csv (built by
scripts/build_sleeve_reference.py with the configured leverage and band). The
live result over its first n trading days is placed within the distribution of
ALL n-day windows in that history:

  return    live cumulative return percentile   >=10 ok | 2-10 watch | <2 alert
  drawdown  live max drawdown vs n-day windows  >= 5th pct ok | worse watch | worse than every window alert
  exposure  gross exposure vs the configured leverage
  freshness days since the sleeve last logged a mark

Fewer than MIN_DAYS marks is "collecting": one good or bad month proves nothing.
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REF_PATH = Path(__file__).resolve().parent.parent / "research" / "reference" / "sleeve_ref_returns.csv"
MIN_DAYS = 20
_RANK = {"ok": 0, "collecting": 0, "watch": 1, "alert": 2}


def load_reference(path=REF_PATH):
    """-> (meta dict, daily-return Series) or (None, None) if absent."""
    path = Path(path)
    if not path.exists():
        return None, None
    with open(path) as f:
        head = f.readline()
    meta = dict(kv.split("=") for kv in head.lstrip("# ").split() if "=" in kv)
    df = pd.read_csv(path, comment="#", parse_dates=["date"], index_col="date")
    return meta, df["ret"]


def _windows(ref: pd.Series, n: int):
    """Cumulative return and max drawdown of every n-day window of the reference."""
    g = np.exp(np.concatenate([[0.0], np.cumsum(np.log1p(ref.values))]))      # equity curve from 1.0
    rets = g[n:] / g[:-n] - 1
    dds = []
    for i in range(0, len(g) - n, 3):                                            # stride 3: plenty of windows, cheap
        w = g[i:i + n + 1]
        dds.append((w / np.maximum.accumulate(w)).min() - 1)
    return rets, np.array(dds)


def _check(name, status, message):
    return {"name": name, "status": status, "message": message}


def evaluate(equity_log: list, account: dict, cfg: dict, ref=None, today: date = None) -> dict:
    """equity_log: sm_equity_log rows ({as_of_date, total_value}); account: the
    overview's account dict (gross, equity) or None."""
    today = today or date.today()
    sc = cfg.get("sector_momentum_sleeve", {})
    meta, series = ref if ref is not None else load_reference()
    checks = []

    if series is None:
        return {"status": "collecting", "checks": [_check("Reference", "watch",
                "No backtest reference file - run scripts/build_sleeve_reference.py")], "n_days": 0}
    if (abs(float(meta.get("leverage", 0)) - float(sc.get("leverage", 1.0))) > 1e-9
            or abs(float(meta.get("band", 0)) - float(sc.get("band_pct", 0.0))) > 1e-9):
        checks.append(_check("Reference", "watch",
                             f"Reference built for {meta.get('leverage')}x / {meta.get('band')}% band but config is "
                             f"{sc.get('leverage')}x / {sc.get('band_pct')}% - rerun scripts/build_sleeve_reference.py"))

    vals = [float(r["total_value"]) for r in equity_log]
    n = len(vals) - 1
    if n < MIN_DAYS:
        checks.append(_check("Return", "collecting", f"{max(n, 0)} of {MIN_DAYS} daily marks collected - too early to judge"))
        checks.append(_check("Drawdown", "collecting", "needs the same minimum history"))
    else:
        live_ret = vals[-1] / vals[0] - 1
        live_dd = float((pd.Series(vals) / pd.Series(vals).cummax() - 1).min())
        rets, dds = _windows(series, n)
        pct = float((rets <= live_ret).mean() * 100)
        st = "ok" if pct >= 10 else "watch" if pct >= 2 else "alert"
        checks.append(_check("Return", st, f"{live_ret * 100:+.1f}% over {n} days = {pct:.0f}th percentile of backtest {n}-day windows "
                                           f"(typical {np.median(rets) * 100:+.1f}%)"))
        q05 = float(np.percentile(dds, 5)); worst = float(dds.min())
        st = "ok" if live_dd >= q05 else "alert" if live_dd < worst else "watch"
        checks.append(_check("Drawdown", st, f"worst dip {live_dd * 100:.1f}% (backtest {n}-day windows: 5% of them dip below "
                                             f"{q05 * 100:.1f}%, worst {worst * 100:.1f}%)"))

    if account:
        lev = float(sc.get("leverage", 1.0))
        gross = float(account.get("gross", 0.0))
        if gross > lev * 1.10:
            checks.append(_check("Exposure", "alert", f"gross {gross:.2f}x is above the {lev}x target"))
        elif gross > lev * 1.03:
            checks.append(_check("Exposure", "watch", f"gross {gross:.2f}x drifted above the {lev}x target (rebalance due?)"))
        else:
            checks.append(_check("Exposure", "ok", f"gross {gross:.2f}x vs target {lev}x"))

    if equity_log:
        last = pd.Timestamp(equity_log[-1]["as_of_date"]).date()
        gap = int(np.busday_count(last, today))
        st = "ok" if gap <= 1 else "watch" if gap <= 3 else "alert"
        checks.append(_check("Freshness", st, f"last daily mark {last} ({gap} business day{'s' if gap != 1 else ''} ago)"))
    else:
        checks.append(_check("Freshness", "collecting", "no daily marks logged yet"))

    worst_status = max((c["status"] for c in checks), key=lambda s: _RANK[s]) if checks else "collecting"
    if worst_status == "collecting" and all(c["status"] in ("ok", "collecting") for c in checks) \
            and any(c["status"] == "collecting" for c in checks):
        overall = "collecting"
    else:
        overall = worst_status if worst_status != "collecting" else "ok"
    return {"status": overall, "checks": checks, "n_days": max(n, 0), "min_days": MIN_DAYS}
