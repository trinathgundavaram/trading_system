
# Trading Platform — Build-From-Scratch Guide

This is a reverse-engineered construction plan for the `trading_platform` codebase (~20,000 lines,
80+ Python files). It answers one question: **if you were writing this from an empty folder, in
what order would you create files and functions, and when would you go back and add to a file you
already started?**

It is written as a sequence of numbered **build passes**. Each pass says: which file(s) to touch,
exactly which functions/classes/methods to write in them (real signatures from the actual code,
with a one-line description of what each does), and — critically — which *earlier* files you have
to reopen and extend because this pass needs something they didn't have yet. That reopening pattern
(main → class A → main → class B → back to class A → …) is how this codebase was actually built,
and it's how any system this size gets built in practice: you can't design all 80 files up front,
you build a thin vertical slice, then widen it.

Treat this as a companion to reading the actual code, not a replacement — file/line counts and exact
signatures are noted so you can jump straight to the real file and compare.

---

## How to read this document

- **Pass** = one sitting's worth of work — a file or small group of files, in build order.
- **New file** = a file that doesn't exist yet in this pass.
- **Reopen** = a file you already wrote in an earlier pass that needs new functions/methods added
  now, because a later pass depends on something it didn't provide yet.
- Function signatures are copied from the real code so you can `grep` for them later.
- "REAL / PROXY / PLACEHOLDER" — this codebase is unusually honest about which numbers are backed
  by live data (REAL), approximated from other data (PROXY), or hardcoded neutral defaults with no
  data source wired up yet (PLACEHOLDER). Keep that habit if you rebuild this — it's what lets
  `analytics/decision_replay.py` and the "why did it do that" tooling exist at all.

---

## PART 0 — Before writing any Python: what this system does

A scheduler wakes up every few minutes during market hours, pulls market-wide context (Fear & Greed,
VIX, macro data) and per-ticker data (price, technicals, fundamentals, news, options) from ~10 data
sources, classifies the market regime (BULL/BEAR/CHOPPY/CRISIS), runs each ticker through a 7-bucket
weighted scoring engine gated by 15 hard vetoes, decides BUY/HOLD/SELL, sizes the position, and
either (a) simulates the trade in a paper-trading ledger, (b) places a real order (if explicitly
armed), or (c) writes a markdown prompt for a human to review and execute manually via Claude
Desktop. Every decision — buy, sell, size, veto, threshold — is logged with enough detail that it
can be replayed and second-guessed later, and a slow-moving "learning loop" watches for whether the
rules are actually working and proposes (but never auto-applies) weight changes.

That one paragraph is the spec. Everything below is "how do you turn that paragraph into 80 files
without getting lost."

---

## PASS 1 — Config and skeleton (new files: `requirements.txt`, `.env.template`, `config.yaml`, `config_loader.py`)

Nothing else can be written until there's a place to put settings and a place to read them from.

**`config_loader.py`** (27 lines total in the real repo — keep this file tiny forever):
- `_to_namespace(obj)` — recursively turns nested dicts into `SimpleNamespace` so callers can write
  `cfg.risk.max_daily_loss_usd` instead of `cfg["risk"]["max_daily_loss_usd"]`.
- `load_config_dict() -> dict` — `yaml.safe_load(config.yaml)`, plain dict form.
- `load_config() -> SimpleNamespace` — `_to_namespace(load_config_dict())`, the dot-access form.
- Module constant `CONFIG_PATH` — the raw file path, exported for two later files
  (`rules/risk_rules.py`, `learning/bayesian_updater.py`) that need to hash or rewrite the file
  directly rather than just read it.

Design decision baked in from day one: **no caching**. `load_config()` re-reads the YAML file every
single call. That's what makes every process (scheduler, server, dashboard) pick up a hand-edited
`config.yaml` without a restart. Cheap enough at this scale not to matter.

`config.yaml` at this point just needs a `watchlist` list and a couple of `trading` fields — you'll
add a new top-level section to this file in nearly every later pass (that's normal; config.yaml ends
up as the most-edited file in the whole project, right alongside `storage/database.py`).

---

## PASS 2 — Logging and the database skeleton (new files: `storage/log_setup.py`, `storage/database.py`)

**`storage/log_setup.py`** (67 lines):
- `log_file_path(process_name) -> str` — `output/logs/{process_name}.log`.
- `setup_logging(process_name, level=logging.INFO)` — idempotent, attaches a `RotatingFileHandler`
  (5MB × 3 backups) + console handler to the **root** logger. One log file per *process*
  (scheduler, server) — not shared — because rotation across two OS processes writing the same file
  isn't safe.
- `tail_log_lines(process_name, max_lines=300) -> list[str]` — last N lines, for a future UI Logs tab.

**`storage/database.py`** — start this file now with only what Pass 3 needs, then reopen it in
*every single later pass*. This is the one file where "come back and add a method" happens more than
anywhere else in the codebase — the real file ends at ~2,600 lines and ~140 methods. Build order for
this pass:
- `Database.__init__(self, path=DB_PATH)` — creates the parent dir, a `threading.Lock()`, calls `init_db()`.
- `init_db(self)` — runs a `SCHEMA` string (start it with just `signals`, `positions`, `cycles`,
  `logs`, `trades`), then a chain of migration methods.
- `_conn(self)` (contextmanager) — opens a SQLite connection (30s timeout), tries WAL mode, commits/closes on exit.
- `_add_column_if_missing(self, conn, table, col, coltype)` — race-safe `ALTER TABLE ADD COLUMN`
  that swallows "duplicate column" errors, because multiple processes can start near-simultaneously.
- `log(level, message)`, `recent_logs(limit=20)`.

Everything else in `Database` (positions, signals, paper trading, pattern database, Bayesian
history, champion/challenger, rotation log, screener candidates, source health, UI event outbox...)
gets added exactly when the pass that needs it arrives — they're listed under each later pass below
so you're not tempted to write all 140 methods speculatively now.

---

## PASS 3 — MCP transport foundation (new files: `mcp_clients/base.py`, `engine/cache.py`)

Every external data source in this system (Robinhood, yfinance, Finviz, FRED, a stock scanner...)
talks over the Model Context Protocol, either as a spawned local subprocess (stdio) or a hosted HTTP
server. Build the two transport base classes once, here, before touching any real data source.

**`engine/cache.py`** (55 lines):
- `TTLCache` class — `__init__` (dict + lock), `get(key)`, `set(key, value, ttl_seconds)`, `clear()`.
- Module singleton `cache = TTLCache()` plus the TTL constants every client will import:
  `TTL_FEAR_GREED=900`, `TTL_FRED=3600`, `TTL_VIX=300`, `TTL_SECTOR=900`, `TTL_CALENDAR=3600`,
  `TTL_TICKER=300`, `TTL_TICKER_LITE=600`, `TTL_FINVIZ=900`, `TTL_MAVERICK=300`.
- (Note for your own build: don't also create a second copy of this under `storage/cache.py` and
  forget which one everything imports — that's a real, harmless-but-confusing duplication in the
  original codebase. Pick one location and make every client import from it.)

**`mcp_clients/base.py`** (450 lines — the biggest "foundation" file):
- `SourceCircuitBreaker` class — per-source failure tracker.
  - `__init__(self, name, fail_threshold=3, cooldown_seconds=900)`.
  - `available() -> bool` — True once the cooldown window has elapsed.
  - `record(self, success, error="")` — thread-safe; resets on success, opens the breaker after
    `fail_threshold` consecutive failures; also lazily persists health to
    `storage.database.Database().upsert_source_health(...)` wrapped in try/except (so a DB hiccup
    never breaks the data path — you haven't written `upsert_source_health` yet; add it in Pass 6
    when the Monitor tab needs it, this call just no-ops until then).
- `StdioMCPClient` class — spawns a fresh subprocess per call (no persistent session).
  - `__init__(self, command, args, env=None)`.
  - `async call_tool(self, tool_name, params=None) -> dict | None` — spawn → init session → call →
    parse JSON (falls back to a markdown-table parser for servers that sometimes render OHLCV as a
    pipe table) → one outer 30s hard timeout around the whole spawn+call+teardown → retry once with
    jittered backoff.
  - `async list_tools(self) -> list`.
- `HttpMCPClient` class — same shape, over `streamablehttp_client` instead of stdio.
  - `__init__(self, url)`.
  - `async call_tool(self, tool_name, params=None) -> dict | None`.
- Module functions:
  - `_parse_markdown_table(text) -> list | None` — pipe-table → `list[dict]`.
  - `run_async(coro)` — runs an async coroutine from sync code safely: submits to a daemon thread,
    blocks on `Event.wait(timeout=40)`; if the thread hasn't finished, logs a warning and returns
    `None`, abandoning the stuck thread rather than risking `asyncio.wait_for()`'s own cancellation
    hanging. This one function is what lets every single client below expose a plain synchronous
    method to the rest of the codebase while doing async I/O underneath.

Everything downstream **composes** one of these two transport classes (`self.client = StdioMCPClient(...)`)
rather than subclassing them — keep that pattern.

---

## PASS 4 — First three data clients, simplest to more complex (new files: `mcp_clients/fear_greed.py`, `mcp_clients/yfinance_mcp.py`, `mcp_clients/fred_mcp.py`)

Build clients in order of complexity so each one teaches you the next layer of the pattern.

**`mcp_clients/fear_greed.py`** (33 lines — simplest possible client, no cache, no breaker):
- `FearGreedMCP.__init__(self)` — `self.client = StdioMCPClient("npx", ["-y", "mcp-server-fear-greed@latest"])`.
- `get_index(self) -> dict` — calls the one tool, unwraps the response, returns
  `score/rating/previous_close/previous_week` + 6 sub-indicator scores, defaulting to neutral 50 on failure.
- `_defaults(self)` — the all-neutral fallback dict.

**`mcp_clients/yfinance_mcp.py`** (170 lines — the "dumb pipe" fallback source everything else demotes later):
- `YFinanceMCP.__init__(self)` — `StdioMCPClient("uvx", ["yfmcp@latest"])`.
- `async _get_all(self, ticker, skip_holders_financials=False, skip_options=False, skip_price_history=False) -> dict` — up to 6 concurrent calls (OHLCV, info, news, options, financials, holders).
- `get_all(self, ticker, ...) -> dict` — sync wrapper via `run_async`.
- `get_vix(self) -> float` — `^VIX` price, default 20.0.
- `get_price_history(self, ticker, period="3mo", interval="1d") -> dict`.
- `get_ticker_info(self, ticker) -> dict`.
- `screen_gappers(...) -> dict`, `screen_equity(...) -> dict`, `get_top_in_sector(sector, ...) -> dict`.

**`mcp_clients/fred_mcp.py`** (69 lines — introduces `asyncio.gather` fan-out):
- `FredMCP.__init__(self)` — `StdioMCPClient("node", [FRED_MCP_PATH])`.
- `async _get_macro(self) -> dict` — 4 concurrent calls (Fed funds, CPI×12mo, 2s10s spread,
  unemployment) via `asyncio.gather(return_exceptions=True)`.
- `_extract_value(self, result)`, `_calc_cpi_trend(self, data) -> str` (`rising|falling|stable|unknown`).
- `get_macro(self) -> dict` — sync wrapper with an all-`None`/`unknown` fallback dict.

---

## PASS 5 — Regime engine, standalone (new file: `engine/regime_engine.py`, 167 lines)

Build this now, in isolation, even though nothing calls it yet — it has **zero internal imports**,
and it's the single most-depended-on piece of "business logic" in the entire system (everything from
buy-scoring to position sizing to threshold math eventually reads its output). Getting this
dependency-free is what lets six unrelated files each take a `regime` parameter without importing
each other.

- Dataclass `RegimeState` — `bull_pct`, `bear_pct`, `choppy_pct`, `transition_probability`,
  `crisis_active`, `dominant_regime`, `confidence_gap`, `confidence_level`, `confidence_score`,
  `regime_version`, `calculated_at`.
- Module-level `_current: Optional[RegimeState] = None` — the process-global singleton.
- `calculate(spy_price, spy_sma50, spy_sma200, vix, fg_score, ad_ratio) -> RegimeState` — five
  weighted signals (SPY vs SMA200 ±30, SPY vs SMA50 ±20, VIX ±25, F&G ±20, A/D ±25) each vote
  bull/bear/choppy; normalize to %; `transition_probability` from 3 of the signals; `crisis_active =
  vix>30 and fg_score<20`; mutates and returns the module-global.
- `current_state() -> Optional[RegimeState]` — reads the singleton.
- `transition_size_scalar(state) -> float` — 1.00 down to 0.40 position-size modifier.
- `regime_threshold_adj(state) -> float` — points to add to the buy threshold (a clean bull regime
  *lowers* the bar); flat 20.0 if `crisis_active`.

---

## PASS 6 — Market context, Layer 1 (new file: `engine/market_context.py`, 194 lines)

**Reopen `storage/database.py`**: nothing yet — this pass doesn't need the DB.
**Reopen `engine/cache.py`**: nothing new, just import the existing TTL constants.

- Dataclass `MarketContextData` — ~25 fields: F&G score/rating + 6 sub-scores, VIX level/elevated/high
  flags, yield spread/inversion flag, CPI trend, fed funds rate, unemployment, hours-to-next-macro-event,
  blackout flag/reason, sector leaders/laggards lists, put/call ratio, breadth, `can_trade`/`no_trade_reason`.
- `MarketContext` class:
  - `__init__(self)` — constructs `FearGreedMCP`, `YFinanceMCP`, `StockScannerMCP` (not written
    yet — stub it or skip the scanner call until Pass 8), `FredMCP`.
  - `fetch(self) -> MarketContextData` — fetches all sources in parallel (`ThreadPoolExecutor`, 4
    workers), assembles the dataclass.
  - `_get_fear_greed`, `_get_vix`, `_get_macro`, `_get_market_data` — each cached via the TTL constants.
  - `_parse_market_data(self, raw) -> dict` — sector leaders/laggards, calls `_check_blackout`.
  - `_check_blackout(self, calendar) -> tuple` — scans for CPI/FOMC/NFP/GDP/PCE within a buffer window.
- Module function `evaluate_market_gate(mkt, cfg) -> tuple[bool, str]` — F&G bounds, VIX ceiling,
  macro blackout, kill switch → `(can_trade, reason)`. This is the FIRST market gate; a second,
  finer one (`rules/market_filters.py`) comes in Pass 10.

At this point you can write a throwaway script that prints `MarketContext().fetch()` and see real
data flow end to end. Worth doing — it's the smallest possible "does the plumbing work" checkpoint
before the codebase gets big.

---

## PASS 7 — Remaining data clients (new files: `mcp_clients/finviz_mcp.py`, `mcp_clients/finviz_screen.py`, `mcp_clients/stock_scanner.py`, `mcp_clients/maverick.py`, `mcp_clients/market_data.py`)

**`mcp_clients/finviz_mcp.py`** (199 lines — first client with cache + breaker + bounded thread pool):
- Module functions: `_to_float`, `_derive_technical_rating(row)` (synthesizes Buy/Hold/Sell from
  SMA20/50/200-vs-price + RSI), `_derive_analyst_rating(row)`, `_clean_earnings_date(raw)`.
- `FinvizMCP.__init__(self)` — `SourceCircuitBreaker("finviz", 3, 900)`.
- `_scrape(self, ticker) -> dict` — deferred `import finviz`, `finviz.get_stock(ticker)`.
- `get_fundamentals(self, ticker) -> dict` — 6h cache check → breaker check → `_scrape()` in a
  bounded `ThreadPoolExecutor` (30s timeout, since the `finviz` package passes no timeout of its own)
  → record breaker outcome → cache and return.

**`mcp_clients/finviz_screen.py`** (198 lines — market-wide screening, plus a real upstream-library bug fix):
- `_snap(value, presets) -> int`, `_cell_text(td) -> str`.
- `_patched_get_table(...)` — monkeypatch fixing a real column-misalignment bug in the `finviz`
  package's HTML table parser.
- `_ensure_patched()` — applies the monkeypatch exactly once.
- `_run_screen(filters, signal, order="") -> list`.
- `get_new_highs(min_price=5.0, min_volume=500_000, limit=50) -> list`.

**`mcp_clients/stock_scanner.py`** (123 lines — introduces dynamic tool-discovery):
- `StockScannerMCP.__init__(self)`.
- `async _available_tools(self) -> set` — `list_tools()` cached 24h, so the client stops calling
  tools a server version no longer exposes instead of warning on every call.
- `async _gather_existing`, `async _get_market_data`, `get_market_data`, `async _get_ticker_data`,
  `get_ticker_data(self, ticker) -> dict` (6h cached).

**`mcp_clients/maverick.py`** (146 lines — first HTTP client, fully optional):
- `MaverickMCP.__init__(self)` — `HttpMCPClient("http://127.0.0.1:8003/mcp")`, breaker(3,300).
- `available` (property) — rechecks reachability every 300s rather than only once at startup.
- `_check_available(self) -> bool`.
- `async _get_all(self, ticker) -> dict` — 5 concurrent calls (technical analysis, RSI, MACD,
  support/resistance, news sentiment).
- `get_all(self, ticker) -> dict` — cache → availability → breaker → bounded semaphore (max 2
  concurrent, 60s acquire timeout) → call → record → cache.

**`mcp_clients/market_data.py`** (947 lines — a whole second market-data layer, direct REST, no MCP
protocol at all, that demotes yfinance to fallback-only):
- `_load_dotenv()`, `_get(url, ...)`.
- `_RateLimiter` class.
- Seven provider classes, each key-gated (inert with no API key set): `AlpacaProvider` (primary,
  free real-time IEX), `FinnhubProvider`, `TiingoProvider`, `TwelveDataProvider`,
  `AlphaVantageProvider` (20 calls/day budget), `FMPProvider` (200 calls/day budget),
  `FinanceQueryProvider` (keyless, on by default). Each exposes some subset of `get_quote`,
  `get_daily_bars`, `get_intraday_bars`, `get_news`, `get_movers`.
- `MarketDataRouter` class — the facade: `__init__` instantiates all 7; `any_configured()`,
  `bars_capable()`; `get_quote`, `get_daily_bars`, `get_intraday_bars`, `get_news`,
  `get_analyst_consensus`, `get_last_earnings_date`, `get_recent_downgrade`, `get_consensus_eps` —
  each tries providers in priority order and returns `(data, provider_name)`.
- Module singleton `router = MarketDataRouter()`.

(`mcp_clients/robinhood_mcp.py` and `mcp_clients/trayd_mcp.py` — hold off. They're only needed once
you reach the execution layer, Pass 15.)

---

## PASS 8 — Market breadth + a genuine circular-import decision (new file: `engine/market_breadth.py`, 410 lines)

This pass has a real design trap worth knowing about before you hit it: `market_breadth.py` needs a
`SECTOR_ETF_NAMES` mapping (SPDR ETF ticker → sector name), and the *only* other place that natural
belongs is `engine/screener.py` — which you haven't built yet (it's Pass 17) and which itself needs
`market_breadth.get_sector_return()`. In the real codebase, `market_breadth.py` imports
`SECTOR_ETF_NAMES` from `screener.py` at module-load time, and `screener.py` imports
`market_breadth`'s function back *lazily* (inside a function body) to break the load-time cycle.

**Decision for your rebuild:** define `SECTOR_ETF_NAMES` here, in `market_breadth.py`, now — it's a
constant dict, it doesn't need `screener.py` to exist — and have `screener.py` import it *from*
`market_breadth.py` when you get to Pass 17. Cleaner than the original and avoids the whole problem.

- Module-level: `SECTOR_ETFS` list, `_NEUTRAL` fallback dict, `SECTOR_NAME_TO_ETF`, `_SECTOR_ALIASES`.
- `calculate(spy_price=None, spy_sma50=None) -> dict` — cached (15min) breadth dict: `ad_ratio`,
  `mcclellan`, `pct_above_20ema`, `pct_above_50ema`, `breadth_acceleration`, `nh_nl_ratio`,
  `ad_slope_5d_positive`, `spy_ad_aligned`, `opex_status`, `is_fallback`.
- `_get_all_closes_cached() -> dict`, `_fetch_sector_closes() -> dict` — parallel 3-month closes for
  the 11 sector ETFs + SPY via `YFinanceMCP`.
- `get_sector_return(sector_name) -> dict` — `{return_1d, return_1m}` = sector return minus SPY
  return, same window (this is what `ticker_data_adapter.py` calls for relative-strength fields).
- `get_spy_return_1m() -> float`.
- `_closes(raw)`, `_ema(values, period)`, `_compute_from_closes(per_etf_closes) -> dict` (the real
  math: EMA-based `pct_above_*ema`, `breadth_acceleration`, `ad_ratio` with a "suspect" flag for
  exactly-0-or-1 values, McClellan from EMA19-EMA39 of net breadth, `nh_nl_ratio`).
- `_spy_ad_aligned(...)`, `_opex_status() -> str` (3rd-Friday-of-month calendar logic via `pytz`).

---

## PASS 9 — Ticker analysis, Layer 2 (new files: `engine/ta_fallback.py`, `engine/ticker_analyzer.py`)

**`engine/ta_fallback.py`** (137 lines — build this first, it's a dependency of the next file):
- `_FallbackTA` class, registered as a pandas `.ta` accessor — drop-in replacement for `pandas_ta`
  when the real package fails to import (a documented real bug: `pandas_ta`'s PyPI release breaks on
  Python <3.12). Methods, each matching real pandas_ta's column-naming convention exactly: `sma`,
  `ema`, `rsi`, `stoch`, `macd`, `bbands`, `atr`, `obv`, `vwap`, `adx`, `cmf`.

**`engine/ticker_analyzer.py`** (985 lines — the single largest "per-ticker" file; budget real time for this one):
- Dataclass `TickerData` — ~70 fields: identity, price/volume, ~25 technical indicators, provenance
  (`data_sources`, `maverick_data_present`), fundamentals, external ratings, sentiment, options,
  insider activity, FMP-sourced estimate fields, data-quality (`data_quality`, `missing_sources`,
  `stale_indicators`).
- `TickerAnalyzer.__init__(self)` — constructs `YFinanceMCP`, `MaverickMCP`, `FinvizMCP`, `StockScannerMCP`.
- `analyze(self, ticker, market_ctx, cfg=None, lite=False) -> TickerData` — the main entry point:
  cache check → fetch up to 9 sources in parallel → override stale Yahoo price/bid/ask with a
  provider quote if available → `_parse_yfinance` → `_calc_indicators` → `_parse_maverick` →
  `_parse_finviz` → `_parse_scanner` → compute `data_quality` → cache → return.
- `_num(v, default=0.0)` (static).
- `_parse_yfinance(self, td, data)` — Yahoo `info`/`news`/`options` → `TickerData` fields.
- `_calc_indicators(self, td, daily_data, intraday_data)` — the core technical math: SMA/EMA/RSI/
  Stoch/MACD/Bollinger/ATR/ADX/CMF, Donchian-20, calls `_calc_swing_low_avwap`,
  `_calc_earnings_avwap`, `_calc_volatility_compression`, OBV trend + accumulation signals, weekly
  trend flags, manual VWAP, support/resistance. Tracks per-indicator staleness into
  `td.stale_indicators`.
- `_calc_volatility_compression(self, td, df)` — TTM Squeeze, NR7/NR4, inside-day.
- `_consecutive_positive_days(self, macd_hist_series) -> int`.
- `_calc_swing_low_avwap`, `_calc_earnings_avwap` — anchored VWAP variants.
- `_parse_maverick`, `_parse_finviz`, `_parse_scanner` — overlay each optional source's data.
- `_score_sentiment(self, headlines) -> float` — local keyword-based sentiment, no API key.

---

## PASS 10 — The dataclass-to-dict bridge (new file: `engine/ticker_data_adapter.py`, 299 lines)

**Reopen `engine/market_breadth.py`**: nothing new, just import `calculate()`/`get_sector_return()`.

This is the single seam in the whole codebase: everything before this pass is strongly-typed
dataclasses (`TickerData`, `MarketContextData`); everything after this pass — every rule module — is
written against a plain dict. Build this before writing a single rule.

- `ticker_to_dict(td, mkt, cfg) -> dict` — flattens ~50 fields, each one commented REAL / PROXY /
  PLACEHOLDER. Also derives new fields on the fly: `rvol_quality_score` (time-of-day-normalized
  relative volume), `data_completeness_pct`, `news_multiplier`, `rs_vs_spy_1m`, and calls
  `market_breadth.get_sector_return(td.sector)` for `sector_rs_1d`/`sector_rs_1m`/`industry_rs_positive`.
- `market_to_dict(mkt, cfg, spy_td=None) -> dict` — market-wide dict, including the full sector-ETF
  breadth proxy from `market_breadth.calculate(spy_price=..., spy_sma50=...)`, annotated with
  `breadth_proxy_type`/`breadth_coverage`/`breadth_stale`/`ad_ratio_suspect` provenance flags.
- `_session_elapsed_fraction() -> float`, `_data_completeness_pct(td) -> float`.

**Adopt the REAL/PROXY/PLACEHOLDER comment convention starting here** — every field you can't
actually source live gets tagged PLACEHOLDER with a neutral default, not silently dropped. It's what
keeps the rest of the system honest about what it's actually deciding on.

---

## PASS 11 — Rule-layer foundations (new files: `rules/common.py`, `rules/spread_quality.py`, `rules/execution_quality.py`, `rules/dynamic_thresholds.py`, `rules/market_filters.py`, `rules/hard_vetoes.py`)

Build these six in this order — each is a one-hop dependency of the next.

**`rules/common.py`** (44 lines, zero internal deps):
- Dataclass `RuleResult` — `name`, `passed`, `weight=0.0`, `value=None`, `detail=""`.
- Dataclass `Position` — `ticker`, `entry_price`, `entry_time`, `shares`, `dollar_amount`,
  `highest_price`, `current_price`, `unrealized_pnl`, `stop_loss=None`, `take_profit=None`,
  `trailing_high=None`, `status="open"`; classmethod `from_db_row(cls, row)`; property `pnl_pct`.

**`rules/spread_quality.py`** (108 lines, zero internal deps):
- Dataclass `SpreadResult` — `spread_pct`, `tier`, `score_penalty_pct`, `hard_veto`, `reason`.
- `evaluate(ticker_data, mode="swing") -> SpreadResult` — neutral pass-through when bid/ask data is
  missing or implausible (data-quality guard); otherwise buckets spread% into
  excellent/good/acceptable/warning/veto tiers, mode-aware (day trades get a tighter band than swing).

**`rules/execution_quality.py`** (160 lines, imports `rules.spread_quality`):
- Dataclass `ExecutionQualityResult` — `total_score`, `tier`, `components`, `score_adjustment_pct`,
  `size_multiplier`, `reasons`.
- `_band_score(value, bands, key_field, ascending)`, `_tier_for_score(score)`.
- `evaluate(ticker_data, candidate_dollar_amount, cfg, mode="swing") -> ExecutionQualityResult` —
  weighted blend of spread score, dollar-volume band, slippage estimate, liquidity-consistency → a
  score adjustment AND a size multiplier (this is a soft signal, not a veto — the hard spread veto
  lives in `hard_vetoes.py`).

**`rules/dynamic_thresholds.py`** (197 lines, imports `engine.regime_engine`):
- `_breadth_adj(breadth_data) -> tuple[float, str]`.
- `calculate(base_threshold, regime, vix, day_of_week, opex_status, ev_pct=0.0, breadth_data=None,
  ev_measured=False, mode="swing", calendar_enabled=False) -> dict` — combines regime adjustment +
  VIX stress (MAX not sum) + calendar (log-only by default) + transition-probability + mode
  adjustment + breadth tier, caps total at +20% above base, then applies the EV bonus *after* the
  cap; clamps final to [50,85].

**`rules/market_filters.py`** (166 lines, imports `engine.regime_engine.current_state` lazily):
- Dataclass `MarketGateResult` — `can_trade`, `market_score`, `reason`, `blocks`, `breadth_tier`.
- `_breadth_tier(mcclellan, ad_ratio) -> tuple[str, float]`.
- `evaluate(market_data, config) -> MarketGateResult` — starts at score 100, subtracts for VIX/F&G/
  blackout, hard-blocks on `crisis_active`, applies breadth tier penalty, checks a 4-signal crisis
  hard-block (McClellan, A/D, VIX, SPY-vs-200DMA must ALL agree); `can_trade = score >= 40`.
- `_is_blackout(event, setting) -> bool`.

**`rules/hard_vetoes.py`** (196 lines, imports `rules.spread_quality` lazily, `storage.database` lazily):

**Reopen `storage/database.py`**: add `ticker_in_cooldown(ticker)`, `set_re_entry_cooldown(ticker,
hours, exit_reason="")` (you won't call the setter until Pass 15's `confirm_fill.py`, but the
getter is needed now), `record_ticker_data_health(ticker, is_stale_cycle) -> int`,
`get_unhealthy_tickers(...)`.

- Dataclass `VetoResult` — `vetoed`, `reason=""`, `veto_code=""`.
- `check(ticker, ticker_data, market_data, config, mode="swing") -> VetoResult` — checks, in order:
  earnings risk, wide spread, low volume, price out of range, below earnings AVWAP, stale quote,
  kill switch, daily loss, profit lock, re-entry cooldown, day-trade time windows, bad/incomplete
  data, already-open, and a Data Provenance Circuit Breaker (stale-indicator streak).
- `_earnings_risk_score(td) -> float`.

---

## PASS 12 — Expected value + the pattern database (new files: `learning/pattern_database.py`, `analytics/confidence_intervals.py`, `engine/ev_engine.py`, `rules/probabilistic_decision.py`)

This is a detour into the "learning" package, earlier than you might expect — but the buy-scoring
brain in Pass 13 calls `engine/ev_engine.py`, which needs both of these to exist first.

**`analytics/confidence_intervals.py`** (66 lines, zero internal deps — pure stats):
- `wilson_ci(p_hat, n, confidence=0.95) -> tuple[float, float]`.
- `math_sqrt(x) -> float`.
- `clopper_pearson_ci(successes, n, confidence=0.95) -> tuple[float, float]`.
- `bootstrap_ci(samples, stat_fn=np.mean, n_resamples=2000, confidence=0.95, seed=42) -> tuple`.
- `two_proportion_z_test(successes_a, n_a, successes_b, n_b) -> dict`.

**`learning/pattern_database.py`** (181 lines, zero internal deps — a leaf module):
- Module constants `NUMERIC_FEATURES` (27 features), `CATEGORICAL_FEATURES` (13), `ALL_FEATURES`,
  `SIMILARITY_THRESHOLD_BY_COUNT`, `MIN_RECENCY_COUNT_BY_FREQUENCY`.
- `_similarity_threshold(n_candidates) -> float`, `_encode_patterns(patterns, query_features) ->
  tuple`, `_cosine_similarity(a, b) -> float`, `_recency_weight(recorded_at, lambda_decay) -> float`.
- `PatternDatabase(db)` class: `record_entry(ticker, mode, features, trade_id=None) -> int`,
  `close_trade(pattern_id, outcome_pct, hold_hours, exit_reason)`, `find_similar_trades(signal_features,
  mode="SWING", event_frequency="COMMON", regime_filter=None, lambda_decay=None) -> list[dict]` (the
  actual cosine-similarity + recency-weighted match search), `pattern_confidence(similar_trades) -> dict`.

**Reopen `storage/database.py`**: add `add_pattern(ticker, mode, features, trade_id=None) -> int`,
`get_pattern_by_id`, `close_pattern`, `get_patterns(mode=None, ticker=None, closed_only=True) -> list`
— `PatternDatabase` is a thin wrapper around these.

**`engine/ev_engine.py`** (142 lines, imports `analytics.confidence_intervals`, `learning.pattern_database`):
- `get_confidence_label(n) -> str`.
- `calculate_ev(similar_trades, event_frequency="COMMON", target_gain_pct=5.0, stop_loss_pct=5.0) ->
  dict` — `p_win`, `avg_win/loss_pct`, `ev`, Wilson-CI bounds, `p_target_gain`/`p_stop_loss` (each
  with its own CI), `expected_hold_hours`.
- `get_ev_for_signal(db, signal_features, ticker, mode="SWING", event_frequency="COMMON",
  regime_filter=None, target_gain_pct=5.0, stop_loss_pct=5.0) -> dict`.

**`rules/probabilistic_decision.py`** (128 lines, no internal imports — consumes `ev_engine`'s dict shape):
- `decide(ev_result, threshold_passed, final_score_pct, final_threshold_pct, cfg) -> dict` — if
  there's enough pattern-DB history, decides from real probability (`p_win >= min_win_probability`
  AND `ev > min_ev_pct`); otherwise falls back to the plain threshold comparison. Either way the
  returned dict is explicit about which mode produced the decision.

---

## PASS 13 — The buy-side brain (new files: `engine/regime_weight_adaptation.py`, `rules/swing_buy_rules.py`)

**`engine/regime_weight_adaptation.py`** (113 lines) — build this now even though it imports
`learning.bayesian_updater.BUCKET_WEIGHT_BOUNDS`, a file you haven't written yet (Pass 20). For now,
just define `BUCKET_WEIGHT_BOUNDS` as a stub dict in a temporary spot, or write a minimal
`learning/bayesian_updater.py` containing only that one constant — you'll flesh the rest of that file
out in Pass 20.
- `_cfg(cfg)`, `_base_weights(cfg, engine)`, `_live_data_gate_passed(cfg, db) -> tuple` (fails closed
  under 200 closed trades).
- `get_effective_bucket_weights(cfg, regime, db=None, engine="swing_buy") -> dict` — returns
  untouched base weights unless adaptation is enabled AND a live-trade-history gate has passed AND a
  regime-specific delta table exists; then clips to bounds and renormalizes.

**`rules/swing_buy_rules.py`** (1,071 lines — this is the biggest file in `rules/`, budget real time):
- Dataclass `BucketScore` — `name`, `weight`, `points`, `max_points`, `min_pct`, `qualified`,
  `rules_fired`, `qual_mult`, `checklist`.
- `_qualification_multiplier(pct_of_max, min_pct) -> float` — continuous interpolation, not a cliff.
- `_BucketBuilder` helper class — `check(name, condition, points)`, `bucket_score(weight, max_points, min_pct)`.
- `_bucket_weight`, `_bucket_min_pct`, `_detect_asset_class(ticker_data, ticker, config) -> str`.
- Dataclass `SwingScoreResult` — `final_score_pct`, `buckets`, `rules_fired`, `threshold`, `passed`,
  `breakdown`, `ev_result`, `execution_quality`, `asset_class`, `threshold_result`, `probabilistic_decision`.
- `score(ticker_data, market_data, regime, config, mode="swing", db=None, ticker=None) ->
  SwingScoreResult` — the main function. Build the 7 buckets **in this order**, testing each in
  isolation before wiring the next:
  1. **TREND** (SMA200/50/20, EMA9>21, ADX-bullish, Donchian break, above earnings-AVWAP,
     weekly-trend, RS-vs-SPY — capped at 38 combined).
  2. **MOMENTUM** (RSI dual-path, MACD family capped at 18, Stochastic dual-path).
  3. **VOLUME_PA** (RVOL, OBV, CMF, Bollinger dual-path, VWAP, AVWAP-swing-low bounce, accumulation
     family capped at 20).
  4. **EXTERNAL** (Maverick, Finviz, analyst consensus, industry RS, estimate-raised, no-recent-downgrade).
  5. **SENTIMENT_MACRO** (news sentiment, sector RS, F&G range, insider buying, short float, yield curve).
  6. **MARKET_BREADTH** (A/D ratio, %-above-EMA, NH/NL, McClellan, breadth acceleration).
  7. **VOLATILITY_EXPANSION** (TTM squeeze, NR7/NR4, inside day — pure additive bonus, up to +4,
     *not* one of the 6 weighted buckets).
  Then: handle EXTERNAL-unavailable weight redistribution → apply spread + execution-quality
  adjustments → call `ev_engine.get_ev_for_signal` → call `dynamic_thresholds.calculate` → call
  `probabilistic_decision.decide` for the real `should_buy` → optionally compute a "challenger" shadow
  score → return `SwingScoreResult`.
- Compatibility adapter section (only needed once you reach Pass 18's `scheduler.py`, which was
  written against an older shape): `_RuleLike`, `BuyResultCompat`, `_bucket_diagnostic_detail(b)`,
  `from_score_result(result)`, `from_veto(veto)`, `already_open()`.

**Checkpoint:** you can now unit-test the entire buy decision offline — feed `score()` a hand-built
`ticker_data`/`market_data` dict and a `RegimeState`, no live network calls needed. Do this before
moving on; it's much easier to debug the 7-bucket math in isolation than once it's wired into a
live scheduler loop.

---

## PASS 14 — Position sizing, portfolio risk, rotation (new files: `engine/position_sizing.py`, `engine/portfolio_risk.py`, `engine/rotation.py`)

**`engine/position_sizing.py`** (192 lines, imports `engine.regime_engine.transition_size_scalar` lazily):
- Dataclass `PositionSizeResult` — `applicable`, `suggested_size_pct`, `suggested_dollar_amount`,
  `base_allocation_usd`, `factors`, `reasons`, `tier_label`.
- `_score_tier`, `_ev_confidence_multiplier`, `_volatility_multiplier`, `_regime_multiplier`.
- `calculate(buy_result, score_result, ticker_data, regime, cfg, portfolio_risk_result=None) ->
  PositionSizeResult` — score-tier % × EV-confidence × volatility × regime × portfolio-risk ×
  execution-quality multipliers, clamped to `[min_size_pct, max_size_pct]`. Never places an order —
  purely a suggestion.

**`engine/portfolio_risk.py`** (313 lines, imports `engine.cache`, `storage.database`, `mcp_clients.yfinance_mcp` lazily):

**Reopen `storage/database.py`**: add `log_portfolio_risk(...)`, `get_recent_portfolio_risk_log(limit=50)`.

- Dataclass `PortfolioRiskResult` — `allowed`, `size_multiplier`, `sector`, `themes`,
  `sector_exposure_pct`, `theme_exposure_pct`, `portfolio_beta`, `max_pairwise_correlation`,
  `high_vol_position_count`, `reasons`, `warnings`.
- `_themes_for`, `_scale_for_cap`, `_position_risk_band_pct`, `_fetch_closes` (cached), `_pearson_correlation`.
- `get_pairwise_correlation(ticker_a, ticker_b, lookback_days=60) -> float | None`.
- `PortfolioRiskEngine(db=None)` class: `evaluate(candidate_ticker, candidate_sector,
  candidate_beta, candidate_dollar_amount, candidate_atr_pct, cfg) -> PortfolioRiskResult` — checks
  sector/theme exposure, correlation cluster, aggregate beta, simultaneous high-vol count; returns a
  `size_multiplier` (never a hard block, unless configured to be one).

**`engine/rotation.py`** (144 lines, zero internal imports):

**Reopen `storage/database.py`**: add `log_rotation(book, candidate_ticker, candidate_score,
victim_ticker, victim_health, victim_days_held, reason="")`, `count_recent_rotations(days=7, simulated=True)`.

- `DEFAULTS` dict — `enabled=False`, `min_candidate_score=85.0`, `max_victim_health_score=55.0`,
  `min_hold_days=3.0`, `max_rotations_per_week=2`.
- `_rcfg`, `_days_held`.
- `find_rotation_victim(db, cfg, candidate_ticker, candidate_score, simulated) -> dict | None` —
  guardrails in order, then picks the eligible open position with the *lowest health score* as the
  victim. Never compares entry scores between candidate and victim — only the victim's current health.

---

## PASS 15 — Loop B: managing open positions (new files: `engine/stop_state_machine.py`, `engine/position_health.py`, `engine/mae_mfe_engine.py`, `rules/exit_scorer.py`, `rules/sell_rules.py`, `engine/position_management.py`)

Everything up to Pass 14 was about deciding whether to *open* a position. This pass is entirely
about what happens to a position once it's open — a genuinely separate concern, and every file here
is a leaf or near-leaf, so build them bottom-up.

**`engine/stop_state_machine.py`** (107 lines, zero internal imports):
- Enum `StopState` — `INITIAL_RISK, TRADE_CONFIRMING, BREAKEVEN, PROFIT_PROTECT, TREND_FOLLOWING, THESIS_BROKEN`.
- Dataclass `StopLevel` — `state`, `stop_price`, `stop_reason`, `trail_from`, `calculated_at`.
- `calculate(position, ticker_data, exit_score, config) -> StopLevel` — 6-stage ladder, checked
  top-down from most- to least-favorable (only ever moves the stop in the trade's favor).
- `should_advance(current_stop, new_stop) -> bool`.

**`engine/position_health.py`** (120 lines, zero internal imports):
- Dataclass `PositionHealth` — `score`, `label`, `action`, `components`, `recommendation`.
- `calculate(position, ticker_data, market_data) -> PositionHealth` — 7 weighted components: P&L
  trend (25%), position EV (20%), RS trend (20%), volume (10%), AVWAP relationship (10%), breadth
  delta since entry (10%), time decay (5%).
- `_calculate_position_ev(position, ticker_data) -> float`.

**`engine/mae_mfe_engine.py`** (84 lines, imports `storage.database`):

**Reopen `storage/database.py`**: add `insert_mae_mfe(data)`, `get_recent_mae_mfe(limit=500)`,
`query_mae_winners(setup_type, regime) -> list`.

- `update_live(position_id, ticker_data, entry_price) -> dict`.
- `evaluate_mae_percentile(position, setup_type, regime) -> dict`.
- `record_completed(trade)`.

**`rules/exit_scorer.py`** (353 lines, imports `engine.regime_weight_adaptation` lazily):
- Dataclass `ExitBucketScore` — `name`, `weight`, `points`, `max_points`, `rules_fired`.
- `_DEFAULT_EXIT_BUCKET_WEIGHTS` (TREND_DETERIORATION 25%, MOMENTUM_WEAKNESS 20%,
  VOLUME_DISTRIBUTION 20%, MARKET_CONTEXT 15%, FUNDAMENTAL_RISK 10%, POSITION_HEALTH 10%).
- `_action_for_score(score) -> tuple` — 0-25 HOLD, 26-45 MONITOR, 46-65 TIGHTEN_STOP, 66-80
  REDUCE_POSITION (50%), 81-100 EXIT (100%).
- Dataclass `ExitScoreResult` — `total_score`, `buckets`, `reasons`, `action`, `partial_exit_pct`.
- `calculate(position, ticker_data, market_data, regime, health, mae_eval, time_stop, cfg=None, db=None)
  -> ExitScoreResult` — mirrors the buy-side's bucket design, but every bucket always counts (no
  qualification gate on the sell side).

**`rules/sell_rules.py`** (158 lines, zero internal imports):
- Dataclass `SellResult` — `should_sell`, `triggered_rule`, `reason`, `urgency`, plus legacy neutral
  fields (`exit_score`, `exit_score_threshold`, `contributing`).
- `SellRulesEngine.evaluate(self, td, position, mkt, cfg) -> SellResult` — HARD exits only
  (stop/trailing, take-profit, earnings-approaching, VIX spike), single trigger wins.

**`engine/position_management.py`** (248 lines — "Loop B", the orchestrator for this whole pass):

**Reopen `storage/database.py`**: add `update_position(position_id, updates: dict)`,
`update_position_by_ticker(ticker, updates)`, `get_all_positions(simulated=None)`.

- `_clean(pos) -> dict` — strips None-valued keys so `.get(key, default)` calls downstream see defaults.
- `run_loop_b(ticker_data_cache, mkt, cfg, regime=None, analyzer=None) -> list` — for each open
  position: fetch/reuse `TickerData` → `update_mae_mfe` → compute days-held/risk-per-share/profit-R →
  `position_health.calculate` → `evaluate_mae_percentile` → `_check_time_stop` →
  `exit_scorer.calculate` → `stop_state_machine.calculate`/`should_advance` → `_check_partial_exit` →
  `_evaluate_priority` → persist via `db.update_position`.
- `_check_time_stop`, `_check_partial_exit`, `_evaluate_priority` — the explicit 4-tier hierarchy:
  RISK CONTROL > THESIS BROKEN > EXIT SCORE action tier > PROFIT MANAGEMENT.

---

## PASS 16 — Execution: paper trading, live trading, account sync (new files: `mcp_clients/robinhood_mcp.py`, `mcp_clients/trayd_mcp.py`, `engine/paper_trader.py`, `engine/live_trader.py`, `engine/account_sync.py`)

**`mcp_clients/robinhood_mcp.py`** (230 lines — read-only, zero trading tools by design):

**Reopen `storage/database.py`**: add `upsert_source_health(name, success, error="",
consecutive_failures=0, breaker_open_until=0.0)`, `get_source_health()` — the breaker's `record()`
method (Pass 3) has been calling this all along; it just never had anywhere to write until now.

- `_credentials() -> dict | None`, `get_client() -> RobinhoodMCP` (thread-lock-guarded singleton, so
  the breaker's failure count survives across callers).
- `RobinhoodMCP.__init__(self)` — breaker(3, 1800) — longer cooldown than other sources, since
  repeated failed logins can flag the actual brokerage account.
- `_call(self, tool, params=None, cache_key=None, ttl=TTL_ACCOUNT) -> dict | None` — shared plumbing.
- `get_portfolio`, `get_positions`, `get_position(ticker)`, `get_options_positions`, `get_dividends`,
  `get_accounts`, `get_order_history(ticker=None)`, `get_watchlist`.

**`mcp_clients/trayd_mcp.py`** (462 lines — the ONE place full trade execution actually lives, via a
hosted remote server, separate from the local read-only Robinhood client):
- `_config() -> dict | None`.
- `TraydMCP.__init__`, `configured`, `_ensure_client` (lazy `mcp.ClientSession` + Bearer auth), `_call`.
- Account/portfolio: `get_portfolio`, `get_positions`, `get_quotes`, `get_orders`, `get_account_list`.
- Orders: `place_order(action, symbol, quantity=None, notional=None, limit_price=None,
  order_type="market")`, `place_ladder_order(...)`, `cancel_order`, `cancel_all_orders`.
- `switch_account`, `check_short_availability`, `execute_natural_language(instruction)`.

**`engine/paper_trader.py`** (305 lines, imports `engine.rotation` lazily):

**Reopen `storage/database.py`**: add `get_paper_account()`, `init_paper_account(starting_cash)`,
`adjust_paper_cash(delta, realized_pnl_delta=0.0)`, `reset_paper_account()`,
`log_paper_trade(ticker, side, price, shares, dollar_amount, reason=None, pattern_id=None,
pnl=None, pnl_pct=None, trade_mode=None)`, `get_paper_trades(limit=100)`, `record_paper_equity(snap)`,
`get_paper_equity_history(limit=500)`, `open_position(...)`, `close_position(...) -> dict`,
`get_open_position(ticker, simulated=None)`.

- `is_watch_mode(cfg) -> bool`.
- `ensure_seeded(db, cfg) -> dict` — idempotent: creates the purse, clones the real book into the sim book.
- `execute_buy(db, cfg, ticker, price, position_size=None, pattern_id=None, trade_mode=None,
  buy_score=None, prices=None, pattern_db=None) -> dict`.
- `execute_sell(db, ticker, price, reason, pattern_db=None) -> dict`.
- `_exit_prices(p, cfg) -> tuple`, `check_exit_triggers(position, price, cfg) -> str | None`,
  `snapshot(db, prices=None, cfg=None) -> dict`.

**`engine/live_trader.py`** (359 lines, imports `mcp_clients.base.SourceCircuitBreaker`, `engine.rotation` lazily):
- Module constants `FILL_WAIT_SECONDS=60`, `LIVE_EXECUTION_CONFIRM_PHRASE = "ENABLE LIVE TRADING"`.
- `is_live_execution_enabled(cfg) -> bool`, `is_live_mode(cfg) -> bool` (ALL three gates must be true).
- `_rh()`, `_login() -> bool`, `_account_number`, `_buying_power`, `_wait_for_fill`, `_fill_details`.
- `execute_buy_live(db, cfg, ticker, price, position_size=None, pattern_id=None, trade_mode=None,
  buy_score=None, pattern_db=None) -> dict` — the only function in the codebase that can place a
  real order. Gate-check everything before you let this fire.
- `execute_sell_live(db, cfg, ticker, reason, pattern_db=None) -> dict` — kill switch does NOT block sells.

**`engine/account_sync.py`** (175 lines, imports `engine.live_trader` lazily):
- `_account_number`, `enabled(cfg) -> bool`, `_fetch_remote_positions`.
- `apply_remote_positions(db, remote) -> dict` — imports missing positions, never auto-closes local-only ones.
- `run(db, cfg) -> dict | None` — throttled to once per 15 min.

---

## PASS 17 — The human hand-off: prompt building (new files: `engine/packet_builder.py`, `engine/pattern_features.py`, `engine/rules_catalog.py`)

**`engine/pattern_features.py`** (105 lines, zero internal imports):
- `build_pattern_features(ticker, td, mkt, buy_result, cfg, regime=None, score_result=None) -> dict`
  — maps a live signal onto the pattern-database's 25-feature schema from Pass 12.

**`engine/packet_builder.py`** (470 lines — this is what actually produces `output/trade_prompt.md`;
build this instead of a separate `prompts/` package, see the note at the end of this document):

**Reopen `storage/database.py`**: add `log_signal(ticker, td, buy_result, sell_result=None,
score_result=None, threshold_result=None, ev_result=None, execution_quality=None,
position_size=None, portfolio_risk=None, regime=None, asset_class=None,
probabilistic_decision=None) -> int`, `get_recent_signals(limit=50)`, `latest_signal(ticker)`.

- `_safe_num`, `_bucket_rejection_reason(b) -> str`.
- `build_trade_prompt(ticker_packets, cfg, position_actions=None) -> str` — header + per-ticker
  packets + optional Loop-B position-management section + summary table.
- `build_position_action_packet(action) -> str`.
- `build_ticker_packet(pkt) -> str` — full per-ticker markdown: market context, price, technicals,
  fundamentals, earnings, sentiment, score/threshold, bucket breakdown, "why it failed", execution
  quality, portfolio risk, suggested size, sell signal, current position.

**`engine/rules_catalog.py`** (380 lines, pure static data, no imports) — a **hand-maintained**
documentation dict of the current rule set, tagged REAL/PROXY/PLACEHOLDER throughout, for a future
UI Strategy tab. Not auto-introspected from the actual rule code — you update it by hand whenever you
change a bucket. `get_strategy_catalog() -> dict` bundles it all.

---

## PASS 18 — The candidate-discovery orchestrator (new file: `engine/screener.py`, 1,417 lines — the largest file in the repo)

**Reopen `engine/market_breadth.py`**: nothing new to write — just confirm `SECTOR_ETF_NAMES` lives
there per the Pass 8 decision, and have this file import it from there.

Everything before this pass assumed you already had a list of tickers to look at (a watchlist). This
pass builds the thing that *finds* new tickers worth looking at, before they ever reach the 7-bucket
scorer.

**Reopen `storage/database.py`**: add `upsert_screener_candidate(ticker, mode, score, source=None,
decomposition=None)`, `get_screener_history(ticker, mode)`, `prune_stale_screener_candidates(mode,
stale_after_days=5)`, `record_screener_outcome(ticker, mode, qualified, stale_data_blocked,
buy_pct=None)`, `get_low_quality_screener_tickers(...)`, plus the universe-sweep methods
(`upsert_universe_symbols`, `get_universe_sweep_batch`, `mark_universe_swept`, `universe_count`).

- Dataclasses `ScreenerCandidate`, `ScreenerResult`.
- `run_screener(cfg, mode="swing", regime=None) -> ScreenerResult` — the main entry point: dispatch
  all enabled sources in parallel → `_pre_filter` → `_apply_quality_gate` → `_dedup_keep_best` →
  `_score_candidates` (Discovery Score) → `_allocate_by_quota` → sector-diversity cap → persist to
  the universe table → reserve exploration slots.
- Source implementations, build in roughly this order (real ones first, so you have something
  working before the harder scoring math): `_screen_rs_gainers`, `_screen_volume_surge`,
  `_screen_gap_candidates`, `_screen_premarket_movers`, `_screen_finviz`, `_screen_sector_leaders`,
  `_screen_alpha_movers`, `_screen_fmp_movers`, `_screen_fq_movers`, `_screen_universe_sweep`.
  (`_screen_momentum_not_implemented`, `_screen_insider_not_implemented` are honest stubs — no
  market-wide MCP tool exists for these; don't fake them.)
- Scoring: `_discovery_score(c, bars, spy_bars) -> tuple` (relative-strength 40% + trend-alignment
  25% + volatility-compression 20% + today's %change 15%), `_persistence_bonus(history, current_score)`.
- Plain-Python re-implementations (deliberately not sharing code with `ticker_analyzer.py`, since
  these only need to *rank*, not be exact): `_sma`, `_relative_return`, `_atr`, `_bollinger`,
  `_range_compression_signals`.

---

## PASS 19 — The learning package (new files: `learning/champion_challenger.py`, `learning/walk_forward.py`, `learning/bayesian_updater.py` — now fully fleshed out, `learning/confidence_calibration.py`, `learning/model_versioning.py`, `engine/learning_loop.py`)

**`learning/champion_challenger.py`** (67 lines, imports `analytics.confidence_intervals`):

**Reopen `storage/database.py`**: add `create_challenge`, `record_challenge_trade`, `get_challenge`,
`update_challenge_status`, `get_active_challenges`, `get_all_challenges(limit=50)`.

- `ChampionChallenger(db, cfg)`: `start_challenge(challenger_config) -> str`, `record_trade(challenge_id,
  is_challenger, won, pnl_pct)`, `evaluate(challenge_id) -> dict` (two-proportion z-test once both
  sides clear a minimum sample), `promote`, `discard`.

**`learning/walk_forward.py`** (99 lines, imports `analytics.confidence_intervals`, `learning.pattern_database` lazily):
- `rule_attribution(patterns, rule_name) -> dict` — win rate when a rule fired vs overall.
- `feature_stability(patterns, rule_name) -> dict` — re-runs across 30/90/180-day windows, labels
  STABLE/VOLATILE/SPIKING/SEASONAL.
- `run_walk_forward(db, mode, rule_names) -> dict` — always returns `requires_human_approval: True`.

**`learning/bayesian_updater.py`** (414 lines — you already wrote `BUCKET_WEIGHT_BOUNDS` back in Pass
13; finish the rest of the file now):

**Reopen `storage/database.py`**: add `log_bayesian_update(...)`, `get_weekly_bayesian_change`,
`add_weekly_bayesian_change`, `get_monthly_bayesian_change`, `add_monthly_bayesian_change`,
`get_bayesian_history`, `log_weight_change_provenance(...)`, `get_weight_change_provenance`,
`get_weight_change_history`.

- `_week_start`, `_month_start`, `get_current_bucket_weight(cfg, bucket, mode="swing") -> float`,
  `_config_hash() -> str`, `_record_provenance(...)`.
- `apply_bucket_weight_to_config(...)` — refuses to write unless `challenge_result` proves an
  out-of-sample promotion, or `force=True`. This refusal is the whole safety point of the file.
- `propose_as_challenge(cfg, db, bucket, new_weight_0_100, mode="swing") -> str` — the safe path:
  starts a `ChampionChallenger` shadow test instead of touching live config.
- `apply_challenge_promoted_weight(...)`.
- `BayesianUpdater(db, cfg)`: `_recent_loss_streak`, `propose_update(...)` (computes gates: min
  trades, occurrence threshold, loss-streak halt, weekly/monthly drift caps, bucket bounds — writes
  nothing), `apply_update(proposal)`, `_blocked(...)`.
- `ShadowValidationRequired(Exception)`.

**`learning/confidence_calibration.py`** (87 lines, imports `analytics.confidence_intervals`):
- `_bucket_for(confidence) -> str`, `calibration_from_pairs(pairs) -> dict`,
  `get_calibrated_confidence(raw_confidence, calibration_table) -> dict`,
  `get_calibration_for_bucket(db) -> dict`.

**`learning/model_versioning.py`** (47 lines, zero internal imports):
- Dataclass `ModelVersionSnapshot` — `rule_engine_version, weight_version, regime_version,
  prompt_version, threshold_version, pattern_db_version`.
- `_hash_file(path) -> str`, `get_current_versions(cfg, prompt_template_path=None) ->
  ModelVersionSnapshot`, `versions_as_dict(...)`.

**`engine/learning_loop.py`** (148 lines, imports `learning.champion_challenger`, `learning.walk_forward`):

**Reopen `storage/database.py`**: add `log_learning_run(...)`, `get_last_learning_run(mode=None)`,
`get_recent_learning_runs(limit=20)`.

- `maybe_run(db, cfg, mode="SWING") -> dict | None` — cheap trigger gate (every N closed trades or N
  days); when fired, runs walk-forward + re-evaluates active challenges; never calls
  `bayesian_updater.propose_update()` automatically — that stays a deliberate manual step.
- `_check_trigger`, `_distinct_rule_names`, `_evaluate_active_challenges`.

---

## PASS 20 — Analytics (new files: all of `analytics/`)

Everything in this pass is a pure read-side consumer of data the earlier passes already write —
build these once `storage/database.py`'s signals/trades/pattern_database tables actually have real
rows in them, so you can sanity-check output against something real rather than guessing at shapes.

- `analytics/price_history_utils.py` (135 lines) — `period_for_days_ago`, `_rows`, `_row_close`,
  `_row_date`, `get_closes_series(ticker, days_ago_needed, yf_client=None) -> dict`, `slice_forward`,
  `closes_on_or_after`. (Needed by two files below — build this one first.)
- `analytics/performance.py` (95 lines, imports `confidence_intervals`) — `win_rate_by`,
  `profit_factor`, `sharpe_ratio`, `equity_curve`, `performance_by_regime`, `exit_efficiency`.
- `analytics/decision_replay.py` (233 lines) — `replay_signal(db, ticker=None, date=None,
  signal_id=None) -> dict`, `_fmt_pct`, `render_replay(record) -> str`. Reconstructs "why did it buy
  X on date Y" purely from already-persisted `signals` columns — never re-derives live.
- `analytics/feature_importance.py` (291 lines, imports `performance`, `learning.pattern_database`) —
  `_point_biserial`, `numeric_feature_importance`, `categorical_feature_importance`,
  `feature_importance`, `_parse_ts`, `_regime_distribution`, `_total_variation_distance`,
  `_classify_drift_reason`, `feature_drift`, `rank_all_features(db, mode="SWING",
  include_drift=False) -> dict`.
- `analytics/missed_opportunity.py` (223 lines, imports `price_history_utils`) —
  `find_missed_opportunities`, `simulate_forward_outcome`, `evaluate_missed_opportunities`,
  `missed_opportunity_summary`, `render_missed_opportunity_report`.
- `analytics/regret_analysis.py` (201 lines, imports `price_history_utils`) — `_classify_regret`,
  `analyze_trade_regret`, `build_regret_report`, `render_regret_report`.
- `analytics/trade_attribution.py` (164 lines, zero internal imports) — dataclass
  `AttributionResult`, `_cfg`, `_match_mae_mfe`, `classify_trade`, `attribute_all`.
- `analytics/overfit_risk.py` (286 lines, imports `feature_importance`, `learning.bayesian_updater`) —
  five safeguard checks (`_sample_size_check`, `_feature_importance_check`, `_walk_forward_check`,
  `_champion_challenger_check`, `_bayesian_drift_budget_check`) rolled into one `generate_report`.
- `analytics/opportunity_cost.py` (47 lines) — `track_rejected_signal`, `simulate_rejected_outcome`,
  `opportunity_cost_report`.
- `analytics/override_analytics.py` (49 lines) — `record_override`, `close_override`,
  `override_impact_report`.

**Reopen `storage/database.py`**: add `log_rejected_signal`, `record_simulated_outcome`,
`get_rejected_signals`, `record_override`, `close_override_outcome`, `get_overrides`,
`get_hold_signals`, `save_missed_opportunity_outcome`, `get_missed_opportunity_outcome`,
`save_regret_analysis`, `get_regret_analysis`, `get_regret_analyses`.

---

## PASS 21 — The scheduler: wiring everything together (new file: `scheduler.py`, 1,327 lines)

This is the pass where every previous file gets imported into one place for the first time. You
cannot write this file first — it only makes sense once Passes 1–20 exist, which is exactly why it's
last among the "engine" work.

**Reopen `storage/database.py`**: add `set_cycle_running`, `set_cycle_finished`, `clear_stale_cycle`,
`set_cycle_stage`, `increment_cycle_tickers_done`, `get_cycle_status`, `set_next_cycle_time`,
`save_latest_regime`, `get_latest_regime`, `log_ui_event`, `get_ui_events_since`,
`request_cycle_cancel`, `clear_cycle_cancel`, `is_cycle_cancel_requested`, `log_cycle`,
`increment_cycle`, `record_news_items`, `get_recent_news`, `prune_old_news`.

- `load_config() -> dict`, `is_market_open(cfg) -> bool`.
- `run_cycle(force=False)` — thin try/finally wrapper around `_run_cycle_impl`.
- `_run_screener_now`, `_get_screener_tickers` — cached/throttled wrapper around Pass 18's `run_screener`.
- `_run_cycle_impl(force=False)` — the full cycle body: gate checks → `MarketContext().fetch()` →
  `_calc_regime_and_market_dict` → market gate → screener → parallel per-ticker `_evaluate_ticker` →
  packet writing → `_run_cycle_tail`.
- `_calc_regime_and_market_dict(mkt, cfg) -> (regime, market_dict)`.
- `_evaluate_ticker(ticker, mkt, market_dict, regime, cfg, trading_mode, ticker_data_cache,
  cycle_count, from_screener=False, allow_paper=True)` — one ticker's full veto → score → size →
  execute → log pipeline (this is the function that calls almost everything from Passes 9–17, in order).
- `_run_cycle_tail(...)` — near-miss telemetry, Loop B, background learning loop, prompt/packet
  writing, paper equity snapshot, cycle logging.
- `evaluate_single_ticker(ticker, cfg=None) -> dict | None` — on-demand scoring bypassing the
  market-hours/kill-switch gates (this exists specifically for Pass 23's web UI).
- `_has_open_pattern`, `_classify_hybrid_leg`, `_latest_open_pattern_id`, `_close_due_patterns`,
  `_prune_pending_prompts`, `_effective_scan_interval`.
- `_price_watch_loop()` — a second, faster background thread (~30s) that watches only open positions
  for stop/target/trailing crosses between full cycles.
- `start()` — builds an APScheduler cron job, starts the price-watch thread, runs one cycle
  immediately, then blocks.

---

## PASS 22 — Manual bridges (new files: `confirm_fill.py`, `robinhood_sync.py`, `robinhood_login_test.py`)

These exist because the default workflow is: the automated pipeline writes a prompt, a human reviews
it and places the real trade manually via Claude Desktop, and *then* this codebase needs to be told
what happened.

**`confirm_fill.py`** (287 lines, imports `learning.pattern_database`, `storage.database`, `engine.mae_mfe_engine` lazily):
- `_load_config`, `_most_recent_open_pattern`, `_snapshot_id`.
- `cmd_buy(ticker, price, shares)` — records the real fill, links the open pattern, seeds
  `stop_state_machine`/`position_health` starting fields.
- `cmd_sell(ticker, price)` — closes the position, records MAE/MFE, sets a cooldown, closes the
  linked pattern with the real outcome.
- `cmd_list()`, `main()` — argparse CLI (`buy`/`sell`/`list`).

**`robinhood_sync.py`** (273 lines, imports `mcp_clients.robinhood_mcp`, `storage.database`):
- `_norm_positions`, `cmd_status`, `cmd_positions`, `cmd_seed_paper` (destructive to the paper book only).
- `cmd_reconcile(rh, apply)` — diffs real Robinhood holdings vs local DB (`missing_local`,
  `stale_local`, `mismatched`); `--apply` imports only missing buys through `confirm_fill.cmd_buy()`.
- `main()`.

**`robinhood_login_test.py`** (107 lines) — a one-off interactive login tester so credential/2FA
failures are visible in a terminal instead of silently swallowed by the headless MCP path.

---

## PASS 23 — The two UIs (new files: `main.py`, `server.py`, `ui/index.html`)

Both of these are read-mostly presentation layers over the SQLite database Passes 1–22 already
populate. Build the terminal one first — it's simpler and proves the data model works before you
invest in a full web app.

**`main.py`** (254 lines, imports `scheduler`, `storage.database`; lazily imports `server.app`):
- `KeyReader` class — non-blocking terminal keypress reader.
- `Dashboard` class — `_market_pulse`, `render() -> Layout` (Rich TUI built entirely from
  `storage.database` reads), `copy_prompt`, `open_prompt`, `run()`.
- `_run_scheduler_background()` — runs `scheduler.start()` on a background thread.
- `main()` — default entry point.
- `run_ui()` — `--ui` flag path: imports and serves `server.app` via `uvicorn`, does **not** start
  its own scheduler thread (you're expected to run `scheduler.py` as a second process alongside it).

**`server.py`** (1,002 lines — FastAPI + WebSocket, ~45 routes; build incrementally, starting with
read-only state routes before the action routes):
- Non-route helpers: `_load_config`/`_save_config`, `_auth_token`, `broadcast`, `_event_poll_loop`
  (polls `db.get_ui_events_since()` every 3s), `_market_pulse_from_logs`, `_build_state_payload`,
  `_send_state`, `_paper_prices`, `_run_manual_cycle`.
- Read routes first: `GET /`, `WS /ws`, `GET /api/state`, `/api/signals`, `/api/positions`,
  `/api/paper/summary`, `/api/paper/trades`, `/api/paper/equity_history`, `/api/robinhood/status`,
  `/api/ticker/names`, `/api/ticker/health`, `/api/alerts`, `/api/news`, `/api/logs`, `/api/trades`,
  `/api/learning/runs`, `/api/strategy`, `/api/analytics/performance`,
  `/api/analytics/regime_performance`, `/api/sources`, `/api/status`, `/api/prompt`.
- Action routes second, each token-gated: `POST /api/paper/sell`, `/api/live_execution` (requires the
  typed confirm phrase from `live_trader.py`), `/api/config`, `/api/ticker/validate`,
  `/api/ticker/evaluate_now` (calls `scheduler.evaluate_single_ticker`), `/api/kill_switch`,
  `/api/prompt/copy`, `/api/cycle/run_now` (calls `scheduler.run_cycle(force=True)`),
  `/api/cycle/cancel`, `/api/alerts/{alert_id}/resolve`.

**`ui/index.html`** — the single-page frontend: opens a `WebSocket` to `/ws` for push updates
(`cycle_complete`, `buy_signal`, `urgent_exit`, `data_quality_alert`), falls back to REST polling.
Build one tab at a time against the routes above, in this order: Dashboard/Overview → Signals →
Portfolio → Control (config editing, kill switch, live-execution arming) → Strategy → Analytics →
Monitor → Logs → News → Journal.

---

## PASS 24 — Tests (new files: `tests/test_scoring_sanity.py`, `tests/test_paper_trading.py`, `tests/test_live_trader.py`, `tests/test_rotation.py`, `tests/test_account_sync.py`)

Write these last, against the finished behavior, not first — this codebase's own test suite is a
regression net over Pass 13/16's trickiest invariants, not a TDD scaffold:
- `test_scoring_sanity.py` — every bucket's max score is achievable and tight; an empty setup scores
  near zero; rule-firing never *decreases* score (monotonicity); buckets are independent; a data
  outage renormalizes weight rather than zeroing it; the qualification multiplier is continuous.
- `test_paper_trading.py` — purse math, seeding, cash/position limits, pattern-close linkage, exit-trigger correctness.
- `test_live_trader.py` — entirely against a mocked `robin_stocks`: the confirm-phrase gate, kill
  switch blocking buys but not sells, size caps.
- `test_rotation.py` — every rotation guardrail, one at a time.
- `test_account_sync.py` — import-only semantics, never auto-closing local-only positions.

---

## Appendix A — Things that look like part of the live pipeline but aren't

Two whole packages exist in the real codebase as earlier iterations that got superseded but never
deleted. Worth knowing about so you don't spend a pass rebuilding dead code:

- **`prompts/` package** (`analysis_prompt.py`, `macro_prompt.py`, `sentiment_prompt.py`,
  `trade_prompt.py`) — an earlier, more verbose approach to building the Claude Desktop prompt.
  Nothing in the live pipeline imports it; `engine/packet_builder.py` (Pass 17) replaced it entirely.
  If you want the exercise of "how would I naively build this the first time," build it before
  `packet_builder.py` — but know you're building throwaway code.
- **`storage/cache.py`** — a near-duplicate `TTLCache` with zero importers anywhere in the repo;
  `engine/cache.py` (Pass 3) is the one everything actually uses. Don't build both — pick one.

## Appendix B — The file you'll touch in almost every pass

`storage/database.py` ends up with ~140 methods across ~35 tables. Every pass above lists exactly
which methods to add to it and why. If you want a single metric for "how far along am I," count
methods in that file against this document — it's the most honest progress bar in the whole project,
because nothing else can be wired up to the rest of the system without a place to persist it first.

## Appendix C — Suggested checkpoints to actually run something

You don't have to wait until Pass 21 to see this system do anything. Good stopping points to run a
throwaway script and eyeball real output:
- After Pass 6: print `MarketContext().fetch()`.
- After Pass 9: print `TickerAnalyzer().analyze("AAPL", mkt)`.
- After Pass 13: hand-build a `ticker_data`/`market_data` dict and call `swing_buy_rules.score(...)` directly.
- After Pass 17: call `build_ticker_packet(...)` on one hand-built packet and read the rendered markdown.
- After Pass 21: run `scheduler.run_cycle(force=True)` once and inspect `output/trade_prompt.md`.
