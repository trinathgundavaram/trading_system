"""Real market-breadth proxy built from the 11 SPDR sector ETFs (XLK, XLF,
XLE, XLV, XLY, XLP, XLI, XLB, XLU, XLRE, XLC), fetched via the yfinance MCP
client already wired up elsewhere in this codebase (mcp_clients/yfinance_mcp.py).
No new MCP server, no new dependency, no API key.

HONESTY NOTE, same spirit as engine/ticker_data_adapter.py and
engine/pattern_features.py: this is a genuine, calculated signal from real
price data - not a fabricated placeholder. But it IS a PROXY for true market
breadth. Real NYSE/Nasdaq advance/decline data covers ~3,000+ issues; this
proxy covers 11 sector-level instruments. It will be noisier and less
granular than a real market-internals feed, but it moves in the right
direction in response to real price action, which is a meaningful upgrade
over the previous static 0.50 / 0.0 / 50.0 / 1.0 placeholders that never
changed regardless of what the market actually did.

If a real breadth data source becomes available later (a market-internals
MCP, IEX, Polygon, etc.), swap the body of calculate() out - the dict shape
(same keys engine/ticker_data_adapter.py's market_to_dict() already produced)
stays the same, so nothing downstream needs to change.
"""
import logging
from datetime import datetime, timedelta

from engine.cache import cache, TTL_SECTOR
from mcp_clients.yfinance_mcp import YFinanceMCP

logger = logging.getLogger(__name__)

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC"]

_NEUTRAL = {
    "ad_ratio": 0.50,
    "ad_ratio_suspect": False,  # neutral defaults are not data artifacts
    "mcclellan": 0.0,
    "pct_above_20ema": 50.0,
    "pct_above_50ema": 50.0,
    "breadth_acceleration": 0.0,
    "nh_nl_ratio": 1.0,
    "ad_slope_5d_positive": True,
    "spy_ad_aligned": True,
}


def calculate(spy_price: float = None, spy_sma50: float = None) -> dict:
    """Cached (TTL_SECTOR = 15 min, same cadence as the old sector-leaders/
    laggards fetch in market_context.py) so a 30-min scan interval doesn't
    re-fetch 11 ETFs' price history every cycle. Falls back to the same
    neutral values the placeholder used if the MCP is unreachable (e.g. no
    network) rather than raising and killing the scan cycle."""
    cached = cache.get("market_breadth")
    result = dict(cached) if cached else None

    if result is None:
        all_closes = _get_all_closes_cached()
        sector_only = {k: v for k, v in all_closes.items() if k != "SPY"}
        if sector_only:
            result = _compute_from_closes(sector_only)
            result["is_fallback"] = False
        else:
            # Data Provenance Circuit Breaker: this IS the fallback path -
            # every value below is the static _NEUTRAL default, not a real
            # reading, because the sector-ETF fetch returned nothing (no
            # network / MCP unreachable). is_fallback lets
            # engine/ticker_data_adapter.py's market_to_dict() and
            # rules/hard_vetoes.py's stale-indicator veto see that BREADTH
            # is stale this cycle, the same way TickerData.stale_indicators
            # already tracks RSI/MACD/TREND/VWAP.
            result = dict(_NEUTRAL)
            result["is_fallback"] = True
        cache.set("market_breadth", result, TTL_SECTOR)

    result = dict(result)
    result["spy_ad_aligned"] = _spy_ad_aligned(result["ad_ratio"], spy_price, spy_sma50)
    result["opex_status"] = _opex_status()
    return result


def _get_all_closes_cached() -> dict:
    """Raw per-symbol close series for the 11 sector ETFs + SPY, cached
    together (TTL_SECTOR) since they're fetched in one parallel batch.
    calculate() (breadth aggregate) and get_sector_return() (per-ticker
    sector relative strength - see below) both read this same cache, so
    adding SPY here costs exactly one extra MCP call per TTL_SECTOR
    refresh (15 min), not one extra call per ticker per cycle."""
    cached = cache.get("sector_etf_closes_raw")
    if cached:
        return cached
    all_closes = _fetch_sector_closes()
    if all_closes:
        cache.set("sector_etf_closes_raw", all_closes, TTL_SECTOR)
    return all_closes


def _fetch_sector_closes() -> dict:
    """Fetched in parallel (ThreadPoolExecutor, same pattern
    engine/market_context.py already uses for its 4 parallel fetches) rather
    than sequentially - each call is independently bounded at ~30-60s by
    mcp_clients/base.py's StdioMCPClient timeout, so 12 sequential calls could
    take several minutes in the worst case (one slow/unreachable server)
    versus roughly one slow call's worth of wall-clock time in parallel.
    Includes SPY alongside the 11 sector ETFs (get_sector_return() below
    needs it as the relative-strength baseline) - calculate() filters SPY
    back out before computing the 11-ETF breadth aggregate."""
    from concurrent.futures import ThreadPoolExecutor

    yf = YFinanceMCP()

    def _fetch_one(symbol: str):
        try:
            raw = yf.get_price_history(symbol, period="3mo", interval="1d")
            closes = _closes(raw)
            return symbol, (closes if len(closes) >= 21 else None)
        except Exception:
            return symbol, None

    symbols = SECTOR_ETFS + ["SPY"]
    all_closes = {}
    with ThreadPoolExecutor(max_workers=len(symbols)) as ex:
        for symbol, closes in ex.map(_fetch_one, symbols):
            if closes:
                all_closes[symbol] = closes
    return all_closes


# yfmcp's 11 fixed sector names (from engine/screener.py's SECTOR_ETF_NAMES,
# used there for yfinance_get_top) inverted to ETF-by-name, plus a few
# common naming variants Finviz/yfinance return that don't exactly match
# yfmcp's vocabulary.
from engine.screener import SECTOR_ETF_NAMES  # noqa: E402 (after module-level constants above)

SECTOR_NAME_TO_ETF = {name: etf for etf, name in SECTOR_ETF_NAMES.items()}
_SECTOR_ALIASES = {
    "Financial": "Financial Services",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Staples": "Consumer Defensive",
    "Health Care": "Healthcare",
}


def get_sector_return(sector_name: str) -> dict:
    """1-day and 1-month return for a ticker's own sector, RELATIVE TO SPY
    (sector_rs = sector's own return - SPY's return over the same window) -
    a genuine sector-rotation/leadership signal, not just "is the sector up
    today" (which mostly just reflects the whole market being up). Reuses
    the SAME cached per-ETF+SPY closes calculate() already fetches for the
    breadth proxy (_get_all_closes_cached) - zero extra MCP calls beyond
    what was already happening every TTL_SECTOR (15 min) refresh.

    sector_name is matched against yfmcp's 11 sector names plus
    _SECTOR_ALIASES above. Falls back to neutral (0.0/0.0) if the sector
    isn't recognized or data is unavailable - same posture as calculate()'s
    _NEUTRAL fallback, never raises and kills a ticker's scoring."""
    if not sector_name:
        return {"return_1d": 0.0, "return_1m": 0.0}
    name = _SECTOR_ALIASES.get(sector_name, sector_name)
    etf = SECTOR_NAME_TO_ETF.get(name)
    if not etf:
        return {"return_1d": 0.0, "return_1m": 0.0}

    all_closes = _get_all_closes_cached()
    sector_closes = all_closes.get(etf)
    spy_closes = all_closes.get("SPY")
    if not sector_closes or not spy_closes or len(sector_closes) < 2 or len(spy_closes) < 2:
        return {"return_1d": 0.0, "return_1m": 0.0}

    def _pct_return(closes: list, bars_back: int) -> float:
        bars_back = min(bars_back, len(closes) - 1)
        base = closes[-1 - bars_back]
        return ((closes[-1] - base) / base * 100) if base else 0.0

    sector_1d, spy_1d = _pct_return(sector_closes, 1), _pct_return(spy_closes, 1)
    # ~21 trading days = 1 calendar month
    sector_1m, spy_1m = _pct_return(sector_closes, 21), _pct_return(spy_closes, 21)

    return {
        "return_1d": round(sector_1d - spy_1d, 2),
        "return_1m": round(sector_1m - spy_1m, 2),
    }


def get_spy_return_1m() -> float:
    """SPY's own ~21-trading-day % return, from the SAME cached closes
    calculate() already fetches - zero extra MCP calls. Used by
    engine/ticker_data_adapter.py to compute a per-ticker relative-strength-
    vs-SPY signal (ticker's own 1m return minus this). Returns 0.0 (neutral)
    when SPY data is unavailable, same fallback posture as get_sector_return.
    (2026-07-15, zero-trades audit.)"""
    all_closes = _get_all_closes_cached()
    spy_closes = all_closes.get("SPY")
    if not spy_closes or len(spy_closes) < 2:
        return 0.0
    bars_back = min(21, len(spy_closes) - 1)
    base = spy_closes[-1 - bars_back]
    return round(((spy_closes[-1] - base) / base * 100), 2) if base else 0.0


def _closes(raw) -> list:
    """Same shape yfinance MCP's price-history responses take elsewhere in
    this codebase (see engine/ticker_analyzer.py's _calc_indicators): either
    {"data": [...]} or a bare list of row dicts, columns case-varying."""
    if isinstance(raw, dict):
        rows = raw.get("data") or raw.get("history") or raw.get("prices") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    closes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("close", "Close", "c"):
            if key in row and row[key] is not None:
                try:
                    closes.append(float(row[key]))
                except (TypeError, ValueError):
                    pass
                break
    return closes


def _ema(values: list, period: int) -> list:
    """Standard EMA, seeded with an SMA of the first `period` values (the
    usual approach when you don't have enough history to burn in a longer
    warm-up window)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _compute_from_closes(per_etf_closes: dict) -> dict:
    n = len(per_etf_closes)

    # pct_above_20ema / pct_above_50ema: real EMA computed per sector ETF
    above_20 = above_50 = n20 = n50 = 0
    for closes in per_etf_closes.values():
        ema20 = _ema(closes, 20)
        if ema20:
            n20 += 1
            if closes[-1] > ema20[-1]:
                above_20 += 1
        if len(closes) >= 50:
            ema50 = _ema(closes, 50)
            if ema50:
                n50 += 1
                if closes[-1] > ema50[-1]:
                    above_50 += 1
    pct_above_20ema = round(100.0 * above_20 / max(1, n20), 1)
    pct_above_50ema = round(100.0 * above_50 / n50, 1) if n50 else 50.0

    # breadth_acceleration: today's pct_above_20ema minus YESTERDAY's, using
    # the exact same closes already fetched (just evaluated one bar earlier)
    # - zero extra MCP calls. "Breadth went from 55% to 72%" is a much
    # stronger signal than a static "72% today" snapshot alone, since it
    # captures whether participation is expanding or already peaking.
    above_20_y = n20_y = 0
    for closes in per_etf_closes.values():
        if len(closes) < 2:
            continue
        closes_y = closes[:-1]
        ema20_y = _ema(closes_y, 20)
        if ema20_y:
            n20_y += 1
            if closes_y[-1] > ema20_y[-1]:
                above_20_y += 1
    pct_above_20ema_yesterday = round(100.0 * above_20_y / max(1, n20_y), 1) if n20_y else pct_above_20ema
    breadth_acceleration = round(pct_above_20ema - pct_above_20ema_yesterday, 1)

    # ad_ratio: today's advancers / (advancers + decliners) among the 11 -
    # bounded 0-1 to match how rules/hard_vetoes.py, rules/market_filters.py,
    # and engine/regime_engine.py use it (thresholds like 0.30, 0.55, 0.60).
    advancers = sum(1 for c in per_etf_closes.values() if len(c) >= 2 and c[-1] > c[-2])
    decliners = sum(1 for c in per_etf_closes.values() if len(c) >= 2 and c[-1] < c[-2])
    ad_ratio = round(advancers / (advancers + decliners), 3) if (advancers + decliners) else 0.5

    # A/D data-quality guard (2026-07-15): an ad_ratio of EXACTLY 0.0 or 1.0
    # means every one of the 11 sector ETFs moved the SAME direction with zero
    # exceptions. That can happen during a genuine panic/euphoria day, but it's
    # rare enough to warrant scrutiny every time — more commonly it means a
    # still-forming bar (pre-market or first minutes after the open), a stale
    # last-close from a data hiccup, or a network issue that made multiple ETF
    # fetches return the same stale price.
    #
    # Previously this extreme ad_ratio would flow directly into
    # rules/market_filters.py's `ad_ratio < 0.30` hard block and silence the
    # ENTIRE scan — a data artifact triggering a false positive that looked
    # identical to a genuine panic day. Now:
    #   1. The extreme is logged with per-ETF detail so the next occurrence is
    #      provable (real vs. artifact) rather than requiring a guess.
    #   2. ad_ratio_suspect=True is returned alongside the value — downstream
    #      code (market_filters.py, dynamic_thresholds.py) uses this to clip
    #      the A/D leg of any threshold or gate rather than treating the 0.00
    #      as a confirmed genuine reading.
    ad_ratio_suspect = False
    if ad_ratio in (0.0, 1.0):
        ad_ratio_suspect = True
        detail = {sym: (c[-2], c[-1]) for sym, c in per_etf_closes.items() if len(c) >= 2}
        logger.warning(
            f"market_breadth: ad_ratio hit an extreme ({ad_ratio}) — {advancers} advancers / "
            f"{decliners} decliners out of {n} sector ETFs, ZERO exceptions. "
            f"Flagging ad_ratio_suspect=True. Last two closes per ETF (prev, latest): {detail}"
        )

    # daily net-advance/decline series across all 11 ETFs, used for both the
    # McClellan proxy and the 5-day slope check below
    min_len = min(len(c) for c in per_etf_closes.values())
    net_breadth_series = []
    for t in range(1, min_len):
        net = 0
        for closes in per_etf_closes.values():
            c = closes[-min_len:]
            if c[t] > c[t - 1]:
                net += 1
            elif c[t] < c[t - 1]:
                net -= 1
        net_breadth_series.append(net)

    ad_slope_5d_positive = True
    if len(net_breadth_series) >= 6:
        recent = net_breadth_series[-5:]
        ad_slope_5d_positive = (recent[-1] - recent[0]) > 0  # strictly rising, not just non-negative

    # mcclellan: EMA19 - EMA39 of net breadth, rescaled by /n*100 so it lands
    # in roughly the same +-100ish range the real McClellan Oscillator uses
    # (real McClellan is NYSE-wide and unscaled; this is an 11-instrument
    # proxy, so the raw diff is rescaled rather than left at a tiny +-11 range)
    mcclellan = 0.0
    if len(net_breadth_series) >= 19:
        ema19 = _ema(net_breadth_series, 19)
        ema39 = _ema(net_breadth_series, 39) if len(net_breadth_series) >= 39 else []
        if ema19 and ema39:
            mcclellan = round(((ema19[-1] - ema39[-1]) / n) * 100, 1)
        elif ema19:
            # not enough history yet for a true EMA39 - use EMA19 alone as a
            # (noisier, but still real) short-run breadth-momentum proxy
            mcclellan = round((ema19[-1] / n) * 100, 1)

    # nh_nl_ratio: proxy using proximity to the trailing ~3mo high/low (we
    # only have 3 months of daily bars, not a true 52-week window)
    new_highs = new_lows = 0
    for closes in per_etf_closes.values():
        window = closes[-63:]
        hi, lo = max(window), min(window)
        if hi > 0 and closes[-1] >= hi * 0.99:
            new_highs += 1
        if lo > 0 and closes[-1] <= lo * 1.01:
            new_lows += 1
    nh_nl_ratio = round((new_highs + 1) / (new_lows + 1), 2)

    return {
        "ad_ratio": ad_ratio,
        "ad_ratio_suspect": ad_ratio_suspect,   # True when 0.0 or 1.0 — possible data artifact
        "mcclellan": mcclellan,
        "pct_above_20ema": pct_above_20ema,
        "pct_above_50ema": pct_above_50ema,
        "breadth_acceleration": breadth_acceleration,
        "nh_nl_ratio": nh_nl_ratio,
        "ad_slope_5d_positive": ad_slope_5d_positive,
    }


def _spy_ad_aligned(ad_ratio: float, spy_price, spy_sma50) -> bool:
    """Does SPY's own trend agree with the sector-breadth direction? Real
    calculation when spy_price/spy_sma50 are supplied (scheduler.py already
    fetches SPY once per cycle for the regime engine - see scheduler.py);
    defaults to True (neutral/non-blocking) when they aren't available yet."""
    if not spy_price or not spy_sma50:
        return True
    spy_uptrend = spy_price > spy_sma50
    breadth_bullish = ad_ratio > 0.5
    return spy_uptrend == breadth_bullish


def _opex_status() -> str:
    """Real calculation - needs no market data at all, just the calendar, so
    there's no excuse for this one to have ever been a placeholder. Monthly
    options expiration = 3rd Friday of the month. 'opex_week' = the week
    containing that Friday (Mon-Fri), 'post_opex' = the next trading day
    after, else 'normal'.

    Bug fix (2026-07-15): was using naive datetime.now() which returns LOCAL
    system time. OpEx is a US-market concept; using the local clock means
    anyone not in ET (or with the wrong system timezone) could compute the
    wrong 'today' relative to US market hours — e.g. a server in PST would
    roll to the next calendar day ~3 hours before ET does. Now uses
    pytz US/Eastern explicitly, same as is_market_open() in scheduler.py."""
    import pytz
    et = pytz.timezone("US/Eastern")
    today = datetime.now(et).date()
    d = today.replace(day=1)
    fridays = []
    while d.month == today.month:
        if d.weekday() == 4:
            fridays.append(d)
        d += timedelta(days=1)
    if len(fridays) < 3:
        return "normal"
    opex_day = fridays[2]
    week_start = opex_day - timedelta(days=opex_day.weekday())
    week_end = week_start + timedelta(days=4)
    if week_start <= today <= week_end:
        return "opex_week"
    next_trading_day = opex_day + timedelta(days=3 if opex_day.weekday() == 4 else 1)
    if today == next_trading_day:
        return "post_opex"
    return "normal"
