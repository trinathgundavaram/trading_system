"""Base MCP clients - direct Python MCP protocol calls via the official `mcp` SDK.
No Claude subprocess anywhere, no API key, zero Claude usage for data fetching.

Robinhood TRADING is intentionally NEVER wrapped here - trade review and
execution stays a manual, interactive step in Claude Desktop (paste
output/trade_prompt.md). As of 2026-07-15 there IS a local READ-ONLY Robinhood
client (mcp_clients/robinhood_mcp.py, wrapping the `robinhood-mcp` PyPI server) -
it can see portfolio/positions but physically cannot place orders (the server
exposes no trading tools at all), so the no-local-execution design holds.
See README.md.
"""
import asyncio
import logging
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


class SourceCircuitBreaker:
    """Per-data-source circuit breaker (2026-07-15, no-buys-round-2 audit).

    Production evidence: finviz/maverick/scanner/news all went dark on
    2026-07-14 - the exact day the screener started pushing 38-70 tickers
    per cycle - and every dead call still burned its full 45s hard timeout
    per ticker, which is simultaneously why cycle duration ballooned (220s+)
    and why EXTERNAL/SENTIMENT buckets scored near-zero on every signal
    (only the always-true defaults fired). A source that has failed several
    times in a row is almost certainly rate-limited/banned/down; hammering
    it again on the very next ticker both wastes the timeout AND (for
    scraper-backed sources like finviz) makes the ban worse.

    After `fail_threshold` consecutive failures the source is marked down
    for `cooldown_seconds` - callers check available() and skip instantly
    (returning their normal empty-dict fallback) instead of waiting out a
    45s timeout per call. One probe call is allowed after the cooldown; a
    success fully closes the breaker."""

    def __init__(self, name: str, fail_threshold: int = 3, cooldown_seconds: int = 900):
        self.name = name
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._down_until = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return time.time() >= self._down_until

    def record(self, success: bool, error: str = ""):
        with self._lock:
            if success:
                if self._consecutive_failures >= self.fail_threshold:
                    logger.info(f"{self.name}: circuit breaker closed - source healthy again")
                self._consecutive_failures = 0
                self._down_until = 0.0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.fail_threshold:
                    self._down_until = time.time() + self.cooldown_seconds
                    logger.warning(
                        f"{self.name}: {self._consecutive_failures} consecutive failures - "
                        f"circuit breaker OPEN, skipping this source for "
                        f"{self.cooldown_seconds / 60:.0f} min (calls return empty instantly "
                        f"instead of burning the timeout per ticker)"
                    )
        # Persist health for the Monitor tab's Data Sources panel
        # (2026-07-15, Trinath: "show me which MCPs are active and which
        # have issues"). Lazy import avoids a circular dependency; any DB
        # hiccup here must never affect the data path itself.
        try:
            from storage.database import Database
            Database().upsert_source_health(
                self.name, success, error=error,
                consecutive_failures=self._consecutive_failures,
                breaker_open_until=self._down_until)
        except Exception:
            pass

    def force_open(self, cooldown_seconds: float, reason: str = ""):
        """Explicitly open the breaker for cooldown_seconds regardless of the
        consecutive-failure count (2026-07-21, FMP 402 fix). For errors that
        are known-permanent for the cooldown window - e.g. HTTP 402/403 'not
        entitled under current subscription' - rather than transient, so we
        stop re-probing every few calls only to hit the same wall, and don't
        let a permanently-broken endpoint eat the same failure budget as a
        genuinely transient one (which self-heals in cooldown_seconds via the
        normal record() path)."""
        with self._lock:
            self._consecutive_failures += 1
            self._down_until = time.time() + cooldown_seconds
            logger.warning(
                f"{self.name}: circuit breaker forced OPEN for "
                f"{cooldown_seconds / 3600:.1f}h{f' ({reason})' if reason else ''} - "
                f"calls return empty instantly instead of retrying a known-permanent failure"
            )
        try:
            from storage.database import Database
            Database().upsert_source_health(
                self.name, False, error=reason,
                consecutive_failures=self._consecutive_failures,
                breaker_open_until=self._down_until)
        except Exception:
            pass


# 45 -> 30 (2026-07-15, cycle-runtime fix): a data call that hasn't answered
# in 30s isn't coming back with anything worth blocking a scan slot for -
# and dead sources are now handled by circuit breakers rather than repeated
# timeout burns.
CALL_TOOL_HARD_TIMEOUT = 30  # seconds - see call_tool()'s docstring


def _parse_markdown_table(text: str) -> list | None:
    """2026-07-14: `uvx yfmcp@latest` always resolves whatever the newest
    published yfmcp release is (no version pin) - discovered via real
    production logs (the "MCP non-JSON response" warning added earlier this
    session) that the currently-resolved version renders
    `yfinance_get_price_history` results as a pandas `to_markdown()` table
    (`| Date | Open | High | Low | Close | Volume | ... |`) instead of JSON.
    Every one of those calls was silently hitting the json.JSONDecodeError
    fallback and being discarded as `{"raw": text}` - which
    engine/ticker_analyzer.py's `_calc_indicators()` can't use, so it fell
    back to stale defaults. This explained a mass STALE_DATA_CIRCUIT_BREAKER
    incident that included TSLA - a mega-cap that should never legitimately
    have thin data - which is what proved this wasn't a stock-selection
    problem at all, it was real, valid OHLCV data being thrown away because
    it arrived in a shape the JSON parser rejected before ever reaching a
    real "is this good data" check.

    Parses a `tabulate`/`DataFrame.to_markdown()`-style pipe table into the
    same `list[dict]` shape a successful JSON response would have produced,
    so it's usable by every existing caller with zero changes on their end.
    Returns None if `text` doesn't look like a markdown table at all (so the
    caller can fall through to its normal non-JSON handling)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 2 or not lines[0].startswith("|"):
        return None

    def _split_row(line: str) -> list:
        inner = line.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        return [cell.strip() for cell in inner.split("|")]

    header = _split_row(lines[0])
    separator_re_chars = set(":-| \t")
    if not header or not all(set(lines[1]) <= separator_re_chars for _ in [0]):
        return None
    if not lines[1].replace("|", "").strip(" :-"):
        pass  # looks like a real separator row - fall through
    elif not set(lines[1]) <= separator_re_chars:
        return None  # second line isn't a markdown separator - not our format

    rows = []
    for line in lines[2:]:
        cells = _split_row(line)
        if len(cells) != len(header):
            continue
        record = {}
        for key, val in zip(header, cells):
            try:
                record[key] = float(val)
            except ValueError:
                record[key] = val
        rows.append(record)
    return rows if rows else None


class StdioMCPClient:
    """Client for stdio-based MCP servers. Spawns `command args...` fresh for
    each call_tool() - simplest correct behavior for a scan-every-N-minutes
    workload; we're not chatty enough to need a persistent session."""

    def __init__(self, command: str, args: list[str], env: dict | None = None):
        # 2026-07-15 (robinhood read-only integration): optional `env` for
        # servers that need credentials (robinhood-mcp reads
        # ROBINHOOD_USERNAME/PASSWORD/TOTP_SECRET from its environment).
        # The MCP SDK does NOT inherit the parent's os.environ by default -
        # with env=None it spawns with get_default_environment() (PATH, HOME,
        # etc. only) - so credentials must be passed explicitly. We merge on
        # top of the SDK default rather than replacing it, because the server
        # still needs PATH/HOME (robin_stocks caches its session token under
        # ~/.tokens/, which requires HOME to resolve).
        if env:
            from mcp.client.stdio import get_default_environment
            merged = get_default_environment()
            merged.update({k: v for k, v in env.items() if v})
            self.server = StdioServerParameters(command=command, args=args, env=merged)
        else:
            self.server = StdioServerParameters(command=command, args=args)

    async def call_tool(self, tool_name: str, params: dict = None) -> dict | None:
        """2026-07-14 hardening: real production evidence showed a cycle
        stuck "running" for 23+ minutes with the log going completely silent
        right after a `yfinance_get_holders`/`yfinance_get_financials` call
        logged "unhandled errors in a TaskGroup (1 sub-exception)" - an
        anyio TaskGroup error, which points at subprocess SPAWN or TEARDOWN
        (inside `async with stdio_client(...)`/`async with ClientSession(...)`
        entering/exiting), not the tool call itself. The two asyncio.wait_for()
        calls below only ever bounded session.initialize()/session.call_tool()
        - the `async with` block's own __aenter__/__aexit__ (process spawn,
        and especially teardown: killing the subprocess, cancelling internal
        reader/writer tasks) had NO timeout at all. If uvx/yfmcp hangs during
        spawn or teardown - much more likely now that this codebase runs up
        to 8 tickers concurrently, each firing several concurrent MCP calls,
        i.e. dozens of concurrent `uvx yfmcp@latest` subprocess spawns instead
        of the old one-at-a-time design - that hang had no escape valve and
        could wedge the calling thread (and therefore the whole cycle, and
        therefore storage/database.py's cycle_status row) forever.

        Fix: wrap the ENTIRE body (spawn + initialize + call + teardown) in
        one outer asyncio.wait_for(). This can't stop a truly stuck OS
        subprocess from continuing to exist, but it guarantees THIS coroutine
        - and therefore scheduler.py's per-ticker thread - always returns
        within CALL_TOOL_HARD_TIMEOUT seconds no matter what happens inside
        the MCP transport, so one bad subprocess can never wedge an entire
        scan cycle again."""
        params = params or {}

        async def _do_call():
            async with stdio_client(self.server) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=30)
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, params), timeout=30
                    )
                    if result.content and result.content[0].text:
                        import json
                        text = result.content[0].text
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            # 2026-07-14: this fallback used to be silent -
                            # zero log line - which is exactly how a real
                            # production incident (100% of tickers showing
                            # every core indicator stale, including TSLA,
                            # during regular market hours) went undiagnosed:
                            # engine/ticker_analyzer.py's _calc_indicators()
                            # expects daily_ohlcv to be a dict-with-"data"-key
                            # or a list; this {"raw": text} shape is neither,
                            # so it silently fell through with no indicator
                            # computed and no error anywhere.
                            #
                            # Same-day follow-up: that first fix only added
                            # visibility - it didn't explain WHY TSLA of all
                            # tickers was non-JSON. Turned out most of these
                            # aren't error pages at all: `uvx yfmcp@latest`
                            # (no version pin) resolved to a release that
                            # renders yfinance_get_price_history as a
                            # markdown table, not JSON - real, valid OHLCV
                            # data that was being discarded as if it were
                            # garbage. Try parsing it as a markdown table
                            # FIRST; only fall back to the raw/rate-limit
                            # flagging below if it genuinely isn't one (e.g.
                            # an actual HTML error page or a plain-text MCP
                            # error message).
                            table = _parse_markdown_table(text)
                            if table is not None:
                                logger.info(
                                    f"MCP response for {tool_name} was a markdown "
                                    f"table, not JSON - parsed {len(table)} rows "
                                    f"of real data instead of discarding it."
                                )
                                return table
                            lowered = text.lower()
                            likely_rate_limited = any(
                                marker in lowered for marker in
                                ("429", "too many requests", "rate limit", "<!doctype html", "<html")
                            )
                            logger.warning(
                                f"MCP non-JSON response for {tool_name}"
                                f"{' (looks like rate-limiting/an error page, NOT real data)' if likely_rate_limited else ''}"
                                f": {text[:200]!r}"
                            )
                            return {"raw": text}
                    return None

        # 2026-07-15 (zero-trades audit): 337 of 810 historic signals were
        # killed by the STALE_DATA_CIRCUIT_BREAKER, and every single one was
        # a TOTAL daily-OHLCV fetch failure (all 5 core indicators stale) -
        # i.e. this call returning None/garbage, most plausibly Yahoo
        # rate-limiting under the concurrent-scan load. One bounded retry
        # with a short jittered backoff recovers the transient cases without
        # meaningfully amplifying load (2 attempts max, only on failure).
        import random
        attempts = 2
        for attempt in range(attempts):
            try:
                result = await asyncio.wait_for(_do_call(), timeout=CALL_TOOL_HARD_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(f"MCP timeout: {tool_name} (hard {CALL_TOOL_HARD_TIMEOUT}s ceiling - "
                                f"likely a hung subprocess spawn/teardown, not just a slow response)")
                result = None
            except Exception as e:
                logger.warning(f"MCP error {tool_name}: {e}")
                result = None
            # Success = anything that isn't None and isn't an unparseable
            # {"raw": ...} error-page fallback.
            if result is not None and not (isinstance(result, dict) and set(result.keys()) == {"raw"}):
                return result
            if attempt < attempts - 1:
                delay = 2.0 + random.random() * 2.0
                logger.info(f"MCP retry for {tool_name} in {delay:.1f}s (attempt {attempt + 2}/{attempts})")
                await asyncio.sleep(delay)
        return result

    async def list_tools(self) -> list:
        async def _do_call():
            async with stdio_client(self.server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools

        try:
            return await asyncio.wait_for(_do_call(), timeout=CALL_TOOL_HARD_TIMEOUT)
        except Exception as e:
            logger.warning(f"list_tools error: {e}")
            return []


class HttpMCPClient:
    """Client for streamable-HTTP MCP servers (MaverickMCP at localhost:8003).
    This project never starts this server - if it's not up, every call below
    returns None and callers degrade gracefully (see mcp/maverick.py)."""

    def __init__(self, url: str):
        self.url = url

    async def call_tool(self, tool_name: str, params: dict = None) -> dict | None:
        """Same outer-timeout hardening as StdioMCPClient.call_tool() (see its
        docstring) - a lower risk here since there's no subprocess spawn/
        teardown involved, but a hung HTTP connection (localhost:8003 not
        responding, e.g. MaverickMCP wedged) had the same "no ceiling on the
        `async with` block itself" gap, so it gets the same fix for
        consistency and the same guarantee."""
        params = params or {}

        async def _do_call():
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=30)
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, params), timeout=30
                    )
                    if result.content and result.content[0].text:
                        import json
                        text = result.content[0].text
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            # Same markdown-table fallback as StdioMCPClient
                            # (see _parse_markdown_table's docstring) - Maverick
                            # hasn't been observed doing this, but there's no
                            # reason to trust every future response is JSON
                            # any more than yfmcp's turned out to be.
                            table = _parse_markdown_table(text)
                            if table is not None:
                                logger.info(
                                    f"HTTP MCP response for {tool_name} was a "
                                    f"markdown table, not JSON - parsed "
                                    f"{len(table)} rows instead of discarding it."
                                )
                                return table
                            return {"raw": text}
                    return None

        try:
            return await asyncio.wait_for(_do_call(), timeout=CALL_TOOL_HARD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"HTTP MCP timeout: {tool_name} (hard {CALL_TOOL_HARD_TIMEOUT}s ceiling)")
            return None
        except Exception as e:
            logger.warning(f"HTTP MCP error {tool_name}: {e}")
            return None


RUN_ASYNC_HARD_TIMEOUT = 40  # seconds (was 60; tracks CALL_TOOL_HARD_TIMEOUT 45->30)


def run_async(coro):
    """Run an async coroutine from sync code (every engine/*.py class below is a
    plain synchronous class so scheduler.py/main.py never has to think about
    asyncio) - safe to call whether or not an event loop is already running.

    2026-07-14 hardening: real production evidence showed server.py's log
    going completely silent for 35+ minutes right after a burst of ~20
    concurrent Maverick (localhost:8003) HTTP MCP sessions with repeated
    "GET stream disconnected, reconnecting" churn - the caller (a
    scheduler.py per-ticker ThreadPoolExecutor worker, which has no event
    loop of its own) was permanently wedged even though call_tool() already
    has a 45s asyncio.wait_for() around it. The reason: asyncio.wait_for()
    times out by CANCELLING the inner task and then AWAITING it to actually
    finish unwinding - if that task doesn't cooperate with cancellation
    (e.g. a nested anyio TaskGroup/SSE-reconnect-loop swallowing
    CancelledError, or a blocking call that can't be interrupted), the
    "cancel and wait for it to really stop" step itself can hang forever,
    silently defeating the nominal timeout. This previously went straight
    into the `except RuntimeError: return asyncio.run(coro)` fallback below
    - which had NO timeout at all - so a defeated inner timeout meant an
    unbounded hang for the calling thread, with nothing left to catch it.

    Fix: every path through this function now submits to a throwaway,
    NON-context-managed single-worker executor and bounds the wait with
    Future.result(timeout=...). Deliberately NOT `with ThreadPoolExecutor()
    as pool:` - discovered via testing that `Executor.__exit__` calls
    `shutdown(wait=True)` unconditionally, including when leaving the `with`
    block because `future.result(timeout=...)` raised TimeoutError, which
    blocks the *caller* until the abandoned worker thread finishes too -
    silently re-introducing the exact unbounded hang this fix exists to
    prevent, just one level up. (This latent bug was already present in the
    pre-2026-07-14 `loop.is_running()` branch below, just never triggered
    visibly.) Calling `pool.shutdown(wait=False)` instead - or simply never
    shutting the throwaway pool down - lets `future.result()`'s TimeoutError
    return control to the caller immediately, leaving the misbehaving
    coroutine's thread to finish (or hang forever) unobserved in the
    background, which is the only way to bound a hang that the timeout
    *inside* the coroutine has already failed to bound."""
    import threading as _threading

    def _submit_and_wait(target_coro):
        # 2026-07-16: plain DAEMON thread instead of a ThreadPoolExecutor.
        # Executor worker threads are non-daemon, and CPython joins every
        # non-daemon thread at interpreter shutdown (threading._shutdown) -
        # so one abandoned wedged call, while no longer hanging the CALLER,
        # would still hang PROCESS EXIT forever (Ctrl-C/restart scripts
        # blocked on a thread that never finishes). A daemon thread is
        # simply killed at exit, which is exactly the semantics an
        # already-abandoned call deserves.
        box = {}
        done = _threading.Event()

        def _runner():
            try:
                box["result"] = asyncio.run(target_coro)
            except Exception as e:
                box["error"] = e
            finally:
                done.set()

        t = _threading.Thread(target=_runner, daemon=True, name="run-async-bounded")
        t.start()
        if not done.wait(timeout=RUN_ASYNC_HARD_TIMEOUT):
            logger.warning(
                f"run_async: hard {RUN_ASYNC_HARD_TIMEOUT}s ceiling hit - "
                f"an inner MCP timeout was defeated (task wouldn't cancel "
                f"cleanly). Abandoning the stuck call and returning None "
                f"so the caller isn't wedged too."
            )
            return None
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # 2026-07-16 (hang forensics, Akhil's 20-40min stuck cycles): the old
    # `loop.run_until_complete(coro)` branch - taken whenever the calling
    # thread had a non-running event loop set - was the ONE path with no
    # hard ceiling. Production logs showed exactly the predicted signature:
    # cycles going silent for 25-35 minutes with ZERO "run_async: hard
    # ceiling" warnings (the bounded paths always log one), right after
    # Maverick teardown/provider-failure churn. A defeated inner timeout on
    # that branch wedged the calling thread forever. Every path is now
    # routed through the hard-bounded throwaway-pool pattern - the ~1-thread
    # cost per call is nothing next to an unbounded hang.
    try:
        return _submit_and_wait(coro)
    except Exception as e:
        logger.warning(f"run_async error: {e}")
        return None
