#!/usr/bin/env python3
"""Fetch real 1-minute bars from Alpaca's REST API - the data-sourcing step
for phase 1 of docs/intraday_momentum_scoping_2026-07-30.md.

RUN THIS ON A MACHINE WITH REAL NETWORK ACCESS TO data.alpaca.markets, NOT
inside a Cowork/sandboxed session - that environment has no route to
Alpaca's API at all (confirmed with curl; see the scoping doc sec 7).
Run it wherever this repo's ALPACA_API_KEY/ALPACA_API_SECRET already work
today for the live/paper path (your own machine, wherever you normally run
this codebase from).

USES SIP, NOT IEX - deliberately different from mcp_clients/market_data.py's
AlpacaProvider, which always requests feed=iex for live quotes/bars because
Alpaca's free tier only allows REAL-TIME iex data without a paid
subscription, and that module's own docstring documents IEX volume being
wildly unrepresentative (routinely 1-3% of real consolidated volume - see
its get_quote() docstring for the incident that found this). This script
is different in kind: it only ever queries HISTORICAL ranges (a backtest's
start/end are by definition in the past), and Alpaca's documented rule is
that free accounts get full SIP (real, consolidated-tape) access for any
query whose end-time is more than 15 minutes old - every range this script
will ever be pointed at satisfies that trivially. Using iex here would
silently reintroduce the exact volume-accuracy problem market_data.py
already diagnosed once, corrupting this strategy's VWAP (a core input to
the band_or_vwap stop mode) and its noise-band volume context - so this
script hard-codes feed=sip and does not expose an iex option.

OUTPUT: a CSV with columns timestamp,open,high,low,close,volume, directly
loadable via scripts/intraday_momentum_sweep.py and
scripts/walk_forward_intraday_momentum.py's --bars-csv (no reshaping
needed - see engine/intraday_momentum_backtest.py's REQUIRED_RAW_COLUMNS).

RESUMABLE: appends to --out incrementally. On restart, reads the existing
file's LAST timestamp and resumes one minute past it, rather than
refetching from --start - a multi-year 1-minute pull is a lot of paginated
requests over a real network connection, and a killed process (laptop
sleep, a dropped connection) shouldn't have to restart from scratch. Uses
--start only when --out doesn't exist yet or is empty.

Usage:
    python3 scripts/fetch_alpaca_minute_bars.py --symbol SPY --start 2016-01-01 --end 2026-07-30 --out spy_1min.csv
    python3 scripts/fetch_alpaca_minute_bars.py --symbol VIXY --start 2016-01-01 --end 2026-07-30 --out vixy_1min.csv
    # VIXY is the VIX proxy this environment settled on - see the scoping doc sec 2/5 item 2:
    # Alpaca carries no real VIX index product, only tradable ETF/ETN symbols.
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta

import requests

BASE = "https://data.alpaca.markets/v2"
FIELDNAMES = ["timestamp", "open", "high", "low", "close", "volume"]


def _load_dotenv():
    """Minimal .env loader (repo root) - same approach as
    mcp_clients/market_data.py's own _load_dotenv, so this script behaves
    identically to the rest of this codebase without adding a new
    dependency. Never overrides a variable already exported in the real
    environment."""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def _to_alpaca_symbol(ticker: str) -> str:
    """Same DOT-notation translation as mcp_clients/market_data.py's
    AlpacaProvider._to_alpaca_symbol - Alpaca's REST API wants 'BRK.B', this
    codebase's canonical form elsewhere is 'BRK-B'. Not load-bearing for
    SPY/VIXY, kept for consistency if this script is ever pointed at a
    class-share ticker."""
    return ticker.replace("-", ".") if "-" in ticker else ticker


def _last_timestamp_in_csv(path: str) -> str | None:
    """Reads only the last line of an existing output CSV to find where to
    resume - doesn't load the whole (potentially multi-million-row) file
    into memory just to find one value."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, 4096)
        f.seek(-block, os.SEEK_END)
        tail = f.read().decode("utf-8", errors="ignore")
    lines = [l for l in tail.strip().splitlines() if l and not l.startswith("timestamp,")]
    if not lines:
        return None
    return lines[-1].split(",")[0]


def fetch_minute_bars(symbol: str, start_iso: str, end_iso: str, key: str, secret: str,
                       progress_cb=None) -> list:
    """Paginates through Alpaca's v2 bars endpoint (next_page_token) from
    start_iso to end_iso (both full RFC3339 timestamps). Returns a list of
    dicts with timestamp/open/high/low/close/volume, ascending. feed is
    hard-coded to 'sip' - see module docstring."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "timeframe": "1Min",
        "start": start_iso,
        "end": end_iso,
        "limit": 10000,
        "adjustment": "split",
        "feed": "sip",
    }
    rows = []
    page = 0
    while True:
        page += 1
        resp = None
        for attempt in range(5):
            resp = requests.get(f"{BASE}/stocks/{_to_alpaca_symbol(symbol)}/bars",
                                 headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"  [rate limited] sleeping {wait}s...", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(f"Alpaca bars request failed after retries: "
                                f"{resp.status_code if resp is not None else '?'} "
                                f"{resp.text[:300] if resp is not None else ''}")
        d = resp.json()
        bars = d.get("bars") or []
        for b in bars:
            rows.append({"timestamp": b["t"], "open": b["o"], "high": b["h"],
                         "low": b["l"], "close": b["c"], "volume": b["v"]})
        if progress_cb:
            progress_cb(page, len(rows), bars[-1]["t"] if bars else None)
        token = d.get("next_page_token")
        if not token:
            break
        params = dict(params, page_token=token)
        # 0.30s = the same 200/min free-tier cap mcp_clients/market_data.py's
        # AlpacaProvider rate-limits to - this script has no reason to be
        # more aggressive against the same account's shared quota.
        time.sleep(0.30)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD, used only when --out doesn't already exist")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    _load_dotenv()
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        print("ALPACA_API_KEY / ALPACA_API_SECRET not found in the environment or .env - this script "
              "needs both (the same keys the live/paper path already uses). If you're seeing this "
              "inside a sandboxed Cowork session: that's expected and not the issue to fix - this "
              "script is meant to run on a machine with real network access to Alpaca instead.",
              file=sys.stderr)
        return 1

    last_ts = _last_timestamp_in_csv(args.out)
    write_header = last_ts is None
    if last_ts:
        resume_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00")) + timedelta(minutes=1)
        start_iso = resume_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"Resuming {args.out}: last saved bar was {last_ts}, continuing from {start_iso}.")
    else:
        start_iso = f"{args.start}T00:00:00Z"
    end_iso = f"{args.end}T23:59:59Z"

    if start_iso >= end_iso:
        print(f"Nothing to fetch - resume point {start_iso} is already at or past --end {args.end}. Done.")
        return 0

    def _progress(page, n_rows, last_bar_ts):
        print(f"  page {page}: {n_rows} rows fetched so far this run, last bar {last_bar_ts}", flush=True)

    print(f"Fetching {args.symbol} 1-min bars, {start_iso} .. {end_iso} (feed=sip)...")
    t0 = time.time()
    rows = fetch_minute_bars(args.symbol, start_iso, end_iso, key, secret, progress_cb=_progress)
    print(f"Fetched {len(rows)} new rows in {time.time() - t0:.1f}s.")

    if rows:
        with open(args.out, "a" if not write_header else "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.out}.")
    else:
        print("No new rows returned (already up to date, or the range/symbol has no data).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
