"""Direct SEC EDGAR client (2026-07-22, Trinath: "unlimited free data source
research - what can be implemented for better data availability").

WHY THIS EXISTS: mcp_clients/stock_scanner.py already tries to source insider
trades from SEC EDGAR, but only THROUGH a third-party `stock-scanner-mcp`
npx server's `edgar_insider_trades` tool - and that module's own 2026-07-22
docstring documents this pipeline "silently contributing ZERO signal" for a
long stretch (tool names/param shapes drifted upstream more than once,
un-pinned `npx -y` resolving to breaking releases, an entirely unverified
response shape). SEC's own EDGAR REST API is free, keyless, and has a
documented, stable JSON/XML schema - going straight to it removes an entire
unreliable middleman (an npm package this platform doesn't control) for
exactly the one field (insider Form 4 transactions) that middleman kept
failing to deliver.

Endpoints used (all api.sec.gov / data.sec.gov / www.sec.gov - no API key,
no account):
  https://www.sec.gov/files/company_tickers.json         ticker -> CIK map
  https://data.sec.gov/submissions/CIK##########.json    per-company filing index
  https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}  filing XML

SEC's fair-access policy requires a descriptive User-Agent identifying the
requester (no key/registration - just a real contact string) and asks for
no more than ~10 requests/second; this module sets EDGAR_USER_AGENT from
.env (falls back to a working default) and rate-limits to ~8/sec.

Feeds engine/ticker_analyzer.py's existing insider-trade parsing in
_parse_scanner() UNCHANGED: get_insider_transactions() returns a list of
dicts already shaped exactly like the generic list-branch that function's
2026-07-22 rewrite expects (transactionCode/transactionShares/
transactionPricePerShare/acquiredDisposedCode/...), so no changes were
needed there - this is a second, first-party source ticker_analyzer.py
prefers ahead of stock-scanner's unverified one, not a replacement of that
parsing logic.

NOT verified against a live network call from where this was written (the
build sandbox's egress is allowlisted and does not include sec.gov) - the
Form 4 XML tag paths below (nonDerivativeTable/nonDerivativeTransaction/...)
match SEC's long-stable, publicly documented ownership-document schema
(https://www.sec.gov/info/edgar/specifications/ownershipxmltechspec.htm),
but test on a real filing before trusting insider_net_direction in
production - same honesty-note convention as every other unverified-shape
integration in this codebase (see mcp_clients/stock_scanner.py, .../
maverick.py's 2026-07-15 wrong-argument-name bug)."""
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 8
TTL_CIK_MAP = 7 * 24 * 3600      # ticker->CIK barely ever changes
TTL_FILINGS = 6 * 3600           # submissions index - Form 4s trickle in daily at most
TTL_INSIDER = 6 * 3600

# SEC fair-access asks for a descriptive contact string identifying the
# requester. It is not a key, but it IS personal data - and in this project's
# case the address also happened to be the Robinhood login identifier, which
# made a hardcoded copy of it a genuine credential disclosure. It moved to
# EDGAR_USER_AGENT in .env (Phase 0 step 0.2). The fallback below is
# deliberately generic: it keeps EDGAR requests working without putting anyone's
# address in a tracked file.
DEFAULT_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "trading_platform research contact-via-repository-owner",
)


def _load_dotenv():
    """Same minimal .env loader as mcp_clients/market_data.py - this module
    is imported independently (e.g. by tests) and can't assume market_data
    already ran its own loader first."""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"edgar_data: .env load failed: {e}")


_load_dotenv()


class _RateLimiter:
    """Same min-interval limiter as market_data.py's - duplicated locally
    (a few lines) rather than cross-imported, so this module stays a
    self-contained provider like every other mcp_clients/*.py file."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval = min_interval_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.time()


_DEBUG_DUMPS_LEFT = 3  # module-level counter - cap how many raw bad-parse
# bodies we write to disk so a systematic failure doesn't spam output/


def _parse_form4_xml(xml_text: str, ticker: str) -> list:
    """Parses one Form 4 ownershipDocument XML into a flat list of
    non-derivative (open-market equity) transactions. Derivative
    transactions (options/RSUs vesting) are intentionally skipped - the
    downstream insider_net_direction signal is about open-market buy/sell
    conviction, which is what nonDerivativeTransaction rows represent."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.info(f"edgar form4 parse error for {ticker}: {e}")
        # 2026-07-22 (first production run - every ticker failing at an
        # identical line:column strongly suggests a systematic issue, not
        # per-filing data quality, but the sandbox this was built in can't
        # reach sec.gov to see the real response body). Dump the raw text
        # that failed to parse so it can be inspected directly - capped at
        # 3 dumps per process lifetime.
        global _DEBUG_DUMPS_LEFT
        if _DEBUG_DUMPS_LEFT > 0:
            _DEBUG_DUMPS_LEFT -= 1
            try:
                import os as _os
                dump_dir = _os.path.join(_os.path.dirname(__file__), "..", "output")
                _os.makedirs(dump_dir, exist_ok=True)
                dump_path = _os.path.join(dump_dir, f"debug_edgar_form4_{ticker}.xml")
                with open(dump_path, "w", encoding="utf-8", errors="replace") as fh:
                    fh.write(xml_text)
                logger.warning(f"edgar: dumped raw failing Form 4 body for {ticker} to {dump_path}")
            except Exception as dump_err:
                logger.info(f"edgar: debug dump failed too: {dump_err}")
        return []

    owner_name = owner_title = ""
    owner_el = root.find(".//reportingOwner")
    if owner_el is not None:
        name_el = owner_el.find(".//reportingOwnerId/rptOwnerName")
        if name_el is not None and name_el.text:
            owner_name = name_el.text.strip()
        title_el = owner_el.find(".//reportingOwnerRelationship/officerTitle")
        if title_el is not None and title_el.text:
            owner_title = title_el.text.strip()

    def _val(tx_el, path):
        el = tx_el.find(path)
        return el.text.strip() if el is not None and el.text else None

    out = []
    for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        code = _val(tx, "transactionCoding/transactionCode")
        if not code:
            continue
        shares = _val(tx, "transactionAmounts/transactionShares/value")
        price = _val(tx, "transactionAmounts/transactionPricePerShare/value")
        ad_code = _val(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
        tx_date = _val(tx, "transactionDate/value")
        try:
            shares_f = float(shares) if shares else 0.0
        except ValueError:
            shares_f = 0.0
        try:
            price_f = float(price) if price else 0.0
        except ValueError:
            price_f = 0.0
        out.append({
            "ticker": ticker,
            "transactionCode": code,
            "transactionShares": shares_f,
            "transactionPricePerShare": price_f,
            "acquiredDisposedCode": ad_code or "",
            "transactionDate": tx_date or "",
            "insiderName": owner_name,
            "insiderTitle": owner_title,
        })
    return out


class EdgarClient:
    name = "edgar"

    def __init__(self):
        self.user_agent = os.getenv("EDGAR_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
        self.breaker = SourceCircuitBreaker("edgar", fail_threshold=3, cooldown_seconds=900)
        self.limiter = _RateLimiter(0.13)  # ~7.5 req/sec, under SEC's ~10/sec fair-access guidance

    def available(self) -> bool:
        return self.breaker.available()

    def _get(self, url: str):
        import requests
        self.limiter.wait()
        r = requests.get(url, headers={"User-Agent": self.user_agent,
                                        "Accept-Encoding": "gzip, deflate"},
                          timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        return r

    def _cik_for(self, ticker: str) -> str | None:
        cik_map = cache.get("edgar_ticker_cik_map")
        if cik_map is None:
            try:
                data = self._get("https://www.sec.gov/files/company_tickers.json").json()
                cik_map = {
                    str(row["ticker"]).upper(): str(row["cik_str"]).zfill(10)
                    for row in data.values()
                    if row.get("ticker") and row.get("cik_str") is not None
                }
                cache.set("edgar_ticker_cik_map", cik_map, TTL_CIK_MAP)
            except Exception as e:
                logger.warning(f"edgar: ticker->CIK map fetch failed: {e}")
                self.breaker.record(False, error=str(e)[:150])
                return None
        return cik_map.get(ticker.upper())

    def get_recent_filings(self, ticker: str, forms: list = None, limit: int = 20) -> list | None:
        """Recent filings for a ticker (any form type), newest first. Each
        row: {form, filingDate, accessionNumber, primaryDocument,
        primaryDocDescription}. `forms` (e.g. ["4"], ["8-K"]) filters the
        cached full list client-side, so one cached submissions.json fetch
        serves every form-type filter for a ticker within TTL_FILINGS."""
        if not self.available():
            return None
        cik = self._cik_for(ticker)
        if not cik:
            return None
        cache_key = f"edgar_filings_{ticker}"
        rows = cache.get(cache_key)
        if rows is None:
            try:
                data = self._get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
                recent = (data.get("filings") or {}).get("recent") or {}
                forms_list = recent.get("form") or []
                n = len(forms_list)
                desc_list = recent.get("primaryDocDescription") or [""] * n
                rows = [
                    {
                        "form": forms_list[i],
                        "filingDate": (recent.get("filingDate") or [""] * n)[i],
                        "accessionNumber": (recent.get("accessionNumber") or [""] * n)[i],
                        "primaryDocument": (recent.get("primaryDocument") or [""] * n)[i],
                        "primaryDocDescription": desc_list[i] if i < len(desc_list) else "",
                    }
                    for i in range(n)
                ]
                cache.set(cache_key, rows, TTL_FILINGS)
                self.breaker.record(True)
            except Exception as e:
                logger.warning(f"edgar filings {ticker}: {e}")
                self.breaker.record(False, error=str(e)[:150])
                return None
        if forms:
            forms_up = {f.upper() for f in forms}
            rows = [r for r in rows if str(r.get("form", "")).upper() in forms_up]
        return rows[:limit]

    def get_insider_transactions(self, ticker: str, max_filings: int = 5, limit: int = 25) -> list | None:
        """Fetches the `max_filings` most recent Form 4s and parses each
        into flat transaction rows (see _parse_form4_xml). Capped at 5
        filings per ticker per TTL window to bound both latency (one
        sequential-ish HTTP fetch per filing, rate-limited) and load on SEC's
        servers - this is called per screener candidate per cycle, same
        caution as every other per-ticker source in this codebase.

        Returns None on total failure (no signal - caller keeps whatever
        stock-scanner's edgar_insider_trades already produced), or a
        (possibly empty) list on success. Empty list is a real answer (no
        recent open-market insider transactions), not a failure - only
        actual fetch/network errors count against the circuit breaker."""
        if not self.available():
            return None
        cache_key = f"edgar_insider_{ticker}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        filings = self.get_recent_filings(ticker, forms=["4"], limit=max_filings)
        if filings is None:
            return None  # fetch itself failed - breaker already recorded it
        if not filings:
            cache.set(cache_key, [], TTL_INSIDER)
            return []  # genuinely no recent Form 4s - not a failure

        cik = self._cik_for(ticker)
        if not cik:
            return None
        cik_plain = str(int(cik))  # archive URLs use the CIK without zero-padding

        out = []
        fetched_ok = False
        for f in filings:
            acc = f.get("accessionNumber")
            if not acc:
                continue
            acc_nodash = acc.replace("-", "")
            # 2026-07-22 (SECOND live-debugging pass - the first "fix" below
            # was wrong, caught by watching a later live cycle still throwing
            # the identical parse error): the original theory was that
            # submissions.json's `primaryDocument` (e.g.
            # "xslF345X05/primary_doc.xml") just needed its "xslNNNNN/"
            # viewer-folder prefix stripped to reach the real XML at the
            # accession root. That's wrong - confirmed by directly fetching a
            # real filing's own index.json (AST SpaceMobile, CIK 1780312,
            # accession 0001493152-25-029456): there is no "primary_doc.xml"
            # at the accession root AT ALL. "xslNNNNN/..." is a pure SEC
            # *viewer route* - it renders HTML for ANY path under it
            # regardless of extension - and stripping the folder just
            # requests a filename that plain doesn't exist in the filing,
            # which is why SEC kept serving back the same rendered HTML.
            # Scanning ~90 real Form 4 filings across several filing agents
            # confirmed the real XML filename varies and is NEVER
            # "primary_doc.xml": "ownership.xml" (the vast majority),
            # "form4.xml", or an agent-specific name like
            # "tm2532056-1_4seq1.xml". There is no way to derive it from
            # `primaryDocument` - it has to be discovered from the filing's
            # own index.json (see _find_form4_xml_filename below).
            try:
                xml_filename = self._find_form4_xml_filename(cik_plain, acc_nodash)
            except Exception as e:
                logger.info(f"edgar form4 index {ticker} {acc}: {e}")
                continue
            if not xml_filename:
                logger.info(f"edgar form4 {ticker} {acc}: no .xml file found in filing index")
                continue
            url = f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{acc_nodash}/{xml_filename}"
            try:
                xml_text = self._get(url).text
                fetched_ok = True
                out.extend(_parse_form4_xml(xml_text, ticker))
            except Exception as e:
                logger.info(f"edgar form4 fetch {ticker} {acc}: {e}")
                continue

        self.breaker.record(fetched_ok, error="" if fetched_ok else "all Form 4 doc fetches failed")
        if not fetched_ok:
            return None
        cache.set(cache_key, out, TTL_INSIDER)
        return out[:limit]

    def _find_form4_xml_filename(self, cik_plain: str, acc_nodash: str) -> str | None:
        """Looks up the REAL raw-XML filename for a filing from its own
        index.json (`directory.item[].name`) rather than guessing from
        submissions.json's `primaryDocument` - see get_insider_transactions'
        2026-07-22 comment for why that guess doesn't work. Picks the first
        `.xml` file that isn't an `-index` variant (Form 4 filings are simple
        enough - one XML doc, sometimes a matching .html rendering, no
        XBRL/exhibit clutter - that "first non-index .xml" is unambiguous in
        practice across every filing agent format seen so far). Cached per
        accession (immutable once filed) for TTL_FILINGS, same window as the
        submissions index itself."""
        cache_key = f"edgar_filing_index_{acc_nodash}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{acc_nodash}/index.json"
        data = self._get(url).json()
        items = ((data.get("directory") or {}).get("item")) or []
        xml_name = None
        for item in items:
            name = str(item.get("name") or "")
            if name.lower().endswith(".xml") and "-index" not in name.lower():
                xml_name = name
                break
        cache.set(cache_key, xml_name or "", TTL_FILINGS)
        return xml_name
