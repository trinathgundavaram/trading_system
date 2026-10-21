#!/usr/bin/env python3
"""Regenerate the backtest reference series the drift monitor compares the live
paper sleeve against (research/reference/sleeve_ref_returns.csv).

Uses the research harness with the CONFIGURED leverage and band (blend of sector
momentum + broad 3/6/12m composite, T-bills when SPY < 200d SMA with hysteresis,
5 bps costs, financing on borrowed cash). Run it again whenever those settings
change, otherwise the monitor reports a reference mismatch.

    python3 scripts/build_sleeve_reference.py
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from config_loader import load_config_dict
from research.data import load_prices
from research.harness import Config, backtest
from research.leverage_study import blend2, levered, momentum, sector_mom
from research.momentum_monthly import CASH
from research.strategies2 import UNIVERSE2
from research.target_return_study import buffered_rule

OUT = Path(__file__).resolve().parent.parent / "research" / "reference" / "sleeve_ref_returns.csv"


def main():
    sc = load_config_dict()["sector_momentum_sleeve"]
    lev, band = float(sc.get("leverage", 1.0)), float(sc.get("band_pct", 0.0))
    P = load_prices(UNIVERSE2 + [CASH], start="2007-06-01").ffill()
    mk = (lambda b: buffered_rule(b, band / 100.0))
    fn = levered(blend2(mk(sector_mom), mk(momentum(8))), lev)
    c = Config(max_leverage=5.0, dd_cap=1.0, financing_bps=float(sc.get("financing_bps", 400.0)),
               cost_bps=float(sc.get("cost_bps", 5.0)))
    r = backtest(fn, P, c)[P.index[c.warmup_days]:]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        f.write(f"# leverage={lev} band={band} start={r.index[0].date()} end={r.index[-1].date()} days={len(r)}\n")
        f.write("date,ret\n")
        for d, v in r.items():
            f.write(f"{d.date()},{v:.8f}\n")
    print(f"wrote {OUT} ({len(r)} days, leverage={lev}, band={band}%)")


if __name__ == "__main__":
    main()
