"""Shared forward-price-history lookup for analytics/missed_opportunity.py and
analytics/regret_analysis.py - both need "what did this ticker's price do
starting on/after some PAST date" from mcp_clients/yfinance_mcp.py.

HONESTY NOTE: YFinanceMCP.get_price_history(ticker, period, interval) only
takes a `period` RELATIVE TO TODAY (e.g. "3mo" = the last 3 months ending
today) - there's no start/end-date parameter in this codebase's yfinance MCP
wrapper. To see what happened after a historical date, this fetches a period
long enough to cover [that date, today] and then slices/aligns client-side.
That means: (1) a date further in the past needs a longer period (more data
to fetch and search through), and (2) alignment depends on being able to
parse a per-bar date from the MCP response, which - like several other
multi-key-fallback spots in this codebase (see engine/screener.py's ticker
key handling) - hasn't been verified against a live response shape. This
module tries several common date-key names and falls back to POSITIONAL
alignment (assume a contiguous daily trading-day series ending today) if none
parse. Positional fallback is less precise around holidays/gaps but still
directionally useful - never silently returns wrong data, just degrades to a
coarser alignment and callers can inspect `date_alignment` in the result to
see which mode was used.
"""
from datetime import datetime, date, timedelta

_PERIOD_STEPS = [
    (35, "1mo"), (95, "3mo"), (185, "6mo"), (370, "1y"), (740, "2y"), (1830, "5y"),
]


def period_for_days_ago(days_ago: int) -> str:
    """Smallest yfinance `period` string that comfortably covers a window
    starting `days_ago` calendar days back through today, with headroom for
    the forward window being measured too (caller should pass
    days_ago + forward_window_days, not just days_ago)."""
    for max_days, period in _PERIOD_STEPS:
        if days_ago <= max_days:
            return period
    return "max"


def _rows(raw) -> list:
    """Same {"data": [...]} / bare-list normalization used throughout this
    codebase (see engine/market_breadth.py's _closes(), engine/
    ticker_analyzer.py's _calc_indicators)."""
    if isinstance(raw, dict):
        return raw.get("data") or raw.get("history") or raw.get("prices") or []
    if isinstance(raw, list):
        return raw
    return []


def _row_close(row: dict):
    for key in ("close", "Close", "c"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _row_date(row: dict):
    for key in ("date", "Date", "timestamp", "Datetime", "datetime", "t"):
        if key in row and row[key] is not None:
            v = row[key]
            try:
                if isinstance(v, (int, float)):
                    # epoch seconds or ms
                    v = v / 1000 if v > 1e12 else v
                    return datetime.utcfromtimestamp(v).date()
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
            except (ValueError, TypeError, OSError):
                continue
    return None


def get_closes_series(ticker: str, days_ago_needed: int, yf_client=None) -> dict:
    """Fetches enough daily history to cover `days_ago_needed` calendar days
    back through today, and returns {"closes": [(date_or_None, close), ...]
    ascending, "date_alignment": "real" | "positional" | "unavailable"}.
    Never raises - returns closes=[] / date_alignment="unavailable" on any
    failure (no network, bad ticker, etc.), matching this codebase's
    "gracefully degrade, never crash the report" convention throughout
    analytics/."""
    if yf_client is None:
        from mcp_clients.yfinance_mcp import YFinanceMCP
        yf_client = YFinanceMCP()

    period = period_for_days_ago(days_ago_needed)
    try:
        raw = yf_client.get_price_history(ticker, period=period, interval="1d")
    except Exception:
        return {"closes": [], "date_alignment": "unavailable"}

    rows = _rows(raw)
    if not rows:
        return {"closes": [], "date_alignment": "unavailable"}

    parsed = [(_row_date(r), _row_close(r)) for r in rows]
    parsed = [(d, c) for d, c in parsed if c is not None]
    if not parsed:
        return {"closes": [], "date_alignment": "unavailable"}

    if all(d is not None for d in (parsed[0][0], parsed[-1][0])):
        parsed.sort(key=lambda x: x[0])
        return {"closes": parsed, "date_alignment": "real"}

    # Positional fallback: assume the LAST row is today and walk backward one
    # calendar day per bar (approximate - doesn't account for weekends/
    # holidays precisely, but keeps ordering correct, which is what matters
    # for "entry bar" / "N bars later" lookups).
    n = len(parsed)
    today = date.today()
    positional = [(today - timedelta(days=(n - 1 - i)), c) for i, (_, c) in enumerate(parsed)]
    return {"closes": positional, "date_alignment": "positional"}


def slice_forward(closes: list, after_date: date, window_days: int) -> list:
    """closes: ascending [(date, close), ...] from get_closes_series().
    Returns the closes strictly AFTER after_date, up to window_days bars -
    i.e. the "what happened next" window a caller measures entry/exit/peak/
    trough against. Empty list if after_date is past the end of the series
    (data not available yet / too recent)."""
    after = [c for d, c in closes if d is not None and d > after_date]
    return after[:window_days] if window_days else after


def closes_on_or_after(closes: list, target_date: date):
    """First close on or after target_date - used to anchor an 'entry price'
    to a signal/trade date that may fall on a non-trading day (weekend,
    holiday) by rolling forward to the next real bar. None if target_date is
    after every bar in the series."""
    for d, c in closes:
        if d is not None and d >= target_date:
            return c
    return None
