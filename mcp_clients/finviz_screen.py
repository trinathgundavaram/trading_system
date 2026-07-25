"""finviz market-wide screening - wraps `finviz.Screener` for
engine/screener.py's `finviz_screen` source.

2026-07-15c: this was left NOT IMPLEMENTED when finviz_mcp.py was ported
to the `finviz` pip package, because a live test of `finviz.Screener` came
back with visibly misaligned columns (Ticker/Company/Sector/etc shifted by
one). Root-caused and fixed here rather than left broken - see
_patched_get_table()'s docstring for the exact bug. Once patched, a live
new-52-week-high screen returns clean, correctly-keyed rows, so this source
is now real.

Chosen signal: `ta_newhigh` (new 52-week highs). This is a genuinely
different discovery lens from every other screener source in
engine/screener.py - rs_gainers/volume_surge/gap_candidates are all same-day
%change/volume screens (yfinance-backed); finviz's new-high signal instead
surfaces price-STRUCTURE breakouts, which none of the yfinance-backed
sources can query for directly (yfmcp's screener fields don't expose
"is this a new 52-week high" as a filter).
"""
import logging
import threading

from lxml import html

from mcp_clients.base import SourceCircuitBreaker
from mcp_clients.finviz_mcp import _call_with_timeout

logger = logging.getLogger(__name__)

CALL_TIMEOUT = 30  # seconds - same rationale as finviz_mcp.py's CALL_TIMEOUT:
                    # the finviz package's http_request_get() passes no
                    # timeout= to requests.get(), so a stalled connection
                    # would otherwise hang forever instead of failing fast.

# Shared with finviz_mcp.py's per-ticker calls would be nicer, but this
# source only makes ONE request per cache window (see engine/screener.py's
# TTL_SCREENER-gated caller), not one per ticker - a dedicated, generously-
# sized semaphore is enough to avoid overlapping with concurrent per-ticker
# finviz_mcp.py calls hitting finviz.com at the same moment.
_CONCURRENCY = threading.Semaphore(1)
# 2026-07-17: this used to be its own permanent `ThreadPoolExecutor(
# max_workers=1)` - with exactly ONE worker, a single stalled scrape (the
# finviz package's http_request_get() has no timeout, see CALL_TIMEOUT's
# comment) permanently wedges 100% of this source's capacity forever, on
# the very first stall. Reuses finviz_mcp.py's `_call_with_timeout()`
# (fresh throwaway daemon thread per call, no shared pool to exhaust)
# instead of keeping a second copy of the same escape-valve logic here.

_breaker = SourceCircuitBreaker("finviz_screen", fail_threshold=3, cooldown_seconds=900)

_PATCHED = False

# finviz's "sh_price_oN" / "sh_avgvol_oN" filters are fixed dropdown presets
# on finviz.com, not arbitrary thresholds - passing a value that isn't one
# of these exact strings gets silently ignored server-side. Snap whatever
# min_price/min_volume config.yaml hands us to the nearest preset rather
# than string-formatting the raw number into the filter code.
_PRICE_PRESETS = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100]
_AVGVOL_PRESETS_K = [50, 100, 200, 300, 400, 500, 750, 1000, 2000]  # thousands


def _snap(value: float, presets: list) -> int:
    return min(presets, key=lambda p: abs(p - value))


def _cell_text(td) -> str:
    """One clean string per <td>, preferring the real ticker/company link
    text over any decorative sibling element - see _patched_get_table()."""
    links = td.cssselect("a.tab-link")
    if links:
        return links[0].text_content().strip()
    return td.text_content().strip()


def _patched_get_table(page_html, headers, rows=None, **kwargs):
    """Drop-in replacement for finviz.helper_functions.scraper_functions.
    get_table(), which is what finviz.Screener uses to turn each result-table
    row into a dict keyed by the column headers.

    THE BUG (confirmed 2026-07-15 against live finviz.com): the stock
    version builds each row with `column.xpath("td//text()")` - an XPath
    that returns a FLAT list of every text node under every <td> in the
    <tr>, with no per-cell boundary. That's fine as long as every <td> has
    exactly one text node. It doesn't: finviz's Ticker cell renders a
    company-logo <a> whose <img> has a one-letter text fallback (e.g.
    `<span>N</span>` inside `<a class="company-ticker">`) BEFORE the real
    `<a class="tab-link">NVVE</a>` ticker link, in the same <td>. That one
    <td> alone contributes 2 text-node entries to the flat list instead of
    1, which shifts every zip(headers, row_data) pairing after it by one
    position for the rest of the row - e.g. 'Ticker' ends up holding just
    'N' (or a concatenation), 'Company' ends up holding the real ticker,
    'Sector' ends up holding the company name, and so on down the row.

    THE FIX: extract text per-<td> (`_cell_text()`, one string per cell -
    exactly how finviz's own `__get_table_headers()` already extracts
    headers via `header_element.text_content()`) instead of per-<tr> text
    nodes, and prefer the real `a.tab-link` text over the logo-fallback span
    specifically in the Ticker cell. Verified against a live 'ta_topgainers'
    and 'ta_newhigh' screen (Overview and Technical tables both): every
    column, including Ticker, comes back correctly aligned.

    Monkeypatched onto finviz.helper_functions.scraper_functions.get_table
    (and finviz.screener's already-imported reference to it) by
    _ensure_patched() before any Screener call - upstream finviz package is
    unmodified on disk, this only patches the in-process function object."""
    if isinstance(page_html, str):
        page_parsed = html.fromstring(page_html)
    else:
        page_parsed = html.fromstring(page_html.text)
    if rows is None:
        rows = -2  # Portfolio's calling convention - unused by Screener but kept for parity

    all_rows = [
        [_cell_text(td) for td in tr.cssselect("td")]
        for tr in page_parsed.cssselect('tr[valign="top"]')
    ]

    data_sets = []
    if rows != -2:
        for row_number, row_data in enumerate(all_rows, 1):
            data_sets.append(dict(zip(headers, row_data)))
            if row_number == rows:
                break
    else:
        for row_data in all_rows:
            data_sets.append(dict(zip(headers, row_data)))
    return data_sets


def _ensure_patched():
    global _PATCHED
    if _PATCHED:
        return
    import finviz.helper_functions.scraper_functions as scrape_mod
    import finviz.screener as screener_mod
    scrape_mod.get_table = _patched_get_table
    screener_mod.scrape.get_table = _patched_get_table  # screener.py imported the module, not the function
    _PATCHED = True
    logger.info("finviz_screen: patched finviz.Screener's row parser (see _patched_get_table docstring)")


def _run_screen(filters: list, signal: str, order: str = "") -> list:
    _ensure_patched()
    from finviz.screener import Screener
    return Screener(filters=filters, signal=signal, order=order).data


def get_new_highs(min_price: float = 5.0, min_volume: int = 500_000, limit: int = 50) -> list:
    """New-52-week-high names, filtered server-side by finviz's own price/
    avg-volume filters so we're not paging through illiquid junk. Returns a
    plain list of {"ticker","price","pct_change","volume"} dicts (already in
    the shape engine/screener.py's _row_to_candidate() looks for) - [] on
    any failure, same fallback contract every other screener source uses.
    """
    if not _breaker.available():
        return []

    price_preset = _snap(min_price, _PRICE_PRESETS)
    avgvol_preset = _snap(min_volume / 1000, _AVGVOL_PRESETS_K)
    filters = [f"sh_price_o{price_preset}", f"sh_avgvol_o{avgvol_preset}"]
    rows = None
    error = ""
    with _CONCURRENCY:
        try:
            rows = _call_with_timeout(_run_screen, filters, "ta_newhigh", "-change", timeout_seconds=CALL_TIMEOUT)
        except TimeoutError as e:
            error = str(e)
        except ImportError:
            error = "`finviz` package not installed - run `pip install finviz`"
        except Exception as exc:
            msg = str(exc)[:160]
            lowered = msg.lower()
            hint = (" <- finviz.com is rate-limiting/blocking this IP"
                    if any(m in lowered for m in ("429", "too many requests", "403", "forbidden"))
                    else "")
            error = f"{type(exc).__name__}: {msg}{hint}"

    ok = rows is not None and error == ""
    _breaker.record(ok, error=error)
    if not ok:
        return []

    out = []
    for row in rows[:limit]:
        ticker = row.get("Ticker")
        if not ticker:
            continue
        pct_raw = str(row.get("Change", "")).replace("%", "").strip()
        try:
            pct = float(pct_raw) if pct_raw else None
        except ValueError:
            pct = None
        vol_raw = str(row.get("Volume", "")).replace(",", "").strip()
        try:
            vol = float(vol_raw) if vol_raw else None
        except ValueError:
            vol = None
        try:
            price = float(row.get("Price")) if row.get("Price") not in (None, "-") else None
        except ValueError:
            price = None
        out.append({"ticker": ticker, "price": price, "pct_change": pct, "volume": vol})
    return out
