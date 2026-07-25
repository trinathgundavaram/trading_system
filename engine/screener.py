"""Auto-discovery stock screener - runs BEFORE the 7-bucket scoring engine
(rules/swing_buy_rules.py) each scan cycle. Generates a candidate universe
from MCP screener tools and feeds tickers into the SAME per-ticker
evaluation path scheduler.py already runs for the hand-curated watchlist
(scheduler._evaluate_ticker) - a screener candidate gets no shortcut through
hard vetoes or scoring, it just gets a chance to be looked at.

Only runs when config.yaml's screener.enabled is true (default false -
"nothing risky by default", same convention as auto_trade/kill_switch).
scheduler.py's run_cycle() imports this module behind that flag.

v2 (deployment-review finding: "it tends to favor stocks that are already
moving today rather than stocks that are about to move" - a screener should
find the best CANDIDATES for the scoring engine, not try to find the best
trade itself). What changed from v1:

  1. DISCOVERY SCORE (see _discovery_score()) - every raw candidate, from
     whichever source found it, is re-ranked on real relative-strength,
     trend-alignment, and volatility-compression evidence, not just today's
     %change/volume. A quiet 20/50/100-day outperformer that isn't gapping
     today can now outrank a one-day spike. This needed one extra real data
     fetch per candidate (yfinance_get_price_history, already a verified
     tool used elsewhere in this codebase) - see _fetch_bars().
  2. PER-SOURCE QUOTAS (see QUOTAS, _allocate_by_quota()) - a fixed slot
     budget per source so one noisy screener (e.g. gap_candidates on a
     volatile day) can't crowd out everything else; leftover slots roll
     over to whichever OTHER candidates scored highest.
  3. REGIME-SCALED CANDIDATE COUNT (see REGIME_MULTIPLIER) - fewer
     candidates surfaced in a BEAR/CRISIS regime (there usually aren't as
     many good long setups), more in a BULL regime, relative to your own
     configured baseline (screener.max_candidates).
  4. SECTOR ROTATION (see _sector_rotation_weights()) - sector_leaders now
     scans the strongest few sectors (by real 1-month sector-vs-SPY
     relative strength, already computed for market_breadth.py) harder than
     the weakest ones, instead of an equal 3-per-sector across all 11.
  5. CANDIDATE PERSISTENCE (see storage/database.py's screener_candidates
     table) - a ticker appearing for the 3rd straight cycle with a rising
     score gets a small additive bonus; a one-off appearance doesn't.
  6. OUTCOME-AWARE LEARNING (2026-07-14 follow-up, see _persistence_bonus()
     and db.record_screener_outcome()/get_low_quality_screener_tickers()) -
     once a screener-sourced ticker has enough real scoring history, the
     persistence bonus (and eventually _pre_filter's exclusion list) is
     driven by what actually happened when it was scored - real qualify
     rate and stale-data-block rate - not just how often it kept
     reappearing. Closes the loop a deployment review flagged: raw
     reappearance alone was rewarding a ticker for showing up, never
     penalizing one that never qualified or was chronically stuck with
     broken data.

HONESTY NOTE, same convention as engine/market_breadth.py and
engine/ticker_analyzer.py's TTM-squeeze work: every source below is tagged
REAL or NOT IMPLEMENTED - nothing here fabricates data.

REAL sources (backed by verified yfmcp tools - see the screen_equity /
screen_gappers / get_top_in_sector wrappers in mcp_clients/yfinance_mcp.py,
whose tool names/params were transcribed from https://pypi.org/project/yfmcp/,
not guessed):
  - rs_gainers          yfinance_screen, sort_field=percentchange
  - volume_surge        yfinance_screen, sort_field=dayvolume
  - gap_candidates       yfinance_screen_gappers
  - pre_market_movers    same tool as gap_candidates, gated to the pre-market
                         window (~4:00-9:30am ET)
  - sector_leaders       yfinance_get_top, once per SPDR sector (rotation-
                         weighted - see above)

The Discovery Score's relative-strength/trend/compression math (below) is a
SEPARATE, deliberately duplicated implementation from
engine/ticker_analyzer.py's - not a refactor of it. ticker_analyzer.py's
indicator calc (pandas_ta-based) is the one the live buy/sell engines score
against and shouldn't be touched for a ranking-only pass; the functions here
are plain-Python rolling-window math (no pandas_ta dependency) over the same
kind of OHLCV bars, used ONLY to rank screener candidates before they reach
the real engine - they are an approximation for prioritization, not a second
copy of the scoring engine itself.

NOT IMPLEMENTED sources: momentum_screen, insider_buying, and an "Earnings
Momentum" screen (EPS surprises/raised guidance/estimate revisions) the
deployment review also asked for. No market-wide screening tool is exposed
by mcp_clients/maverick.py or mcp_clients/stock_scanner.py - each only
exposes PER-TICKER lookups (get_all(ticker), get_ticker_data(ticker), the
latter of which does include per-ticker analyst_ratings/earnings but not a
market-wide "screen everything for a positive surprise" tool). If a real
screener tool shows up on either of those servers later, wire it in the same
way rs_gainers/volume_surge are wired.

finviz_screen IS implemented (2026-07-15c) via mcp_clients/finviz_screen.py,
wrapping finviz.Screener (new-52-week-high breakouts) - see that module's
docstring for the row-parsing bug that had to be fixed first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime

from engine.cache import cache

logger = logging.getLogger(__name__)

# SPDR sector ETF -> yfmcp sector name. Same 11-sector universe
# engine/market_breadth.py's SECTOR_ETFS uses for the breadth proxy, reused
# here so "sector leaders" means the same 11 sectors everywhere in this
# codebase. yfmcp's 11 sector names are its own fixed vocabulary (from the
# yfmcp PyPI docs), not something this codebase invented.
SECTOR_ETF_NAMES = {
    "XLK": "Technology",
    "XLF": "Financial Services",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLY": "Consumer Cyclical",
    "XLP": "Consumer Defensive",
    "XLI": "Industrials",
    "XLB": "Basic Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

TTL_SCREENER = 600   # 10 min - raw source query results
TTL_BARS = 4 * 3600  # 4 hr - daily OHLCV for Discovery Score math; these are
                      # daily bars, they don't need re-fetching every cycle
                      # the way an intraday quote would.


@dataclass
class ScreenerCandidate:
    ticker: str
    source: str
    reason: str = ""
    pct_change: float = None
    volume: float = None
    price: float = None
    priority: int = 5  # lower = higher priority, tie-break only now that
                        # discovery_score does the real ranking
    discovery_score: float = 0.0
    rs_20d: float = None
    rs_50d: float = None
    rs_100d: float = None
    trend_aligned: bool = False
    persistence_bonus: float = 0.0
    sector: str = None  # captured by the quality gate's info fetch (2026-07-15) - feeds the sector-diversity cap
    # verified (2026-07-21, external review - "on quality-gate data failure:
    # mark candidate UNVERIFIED; do not grant priority ranking credit; allow
    # it only if capacity remains after verified candidates"): True when
    # _apply_quality_gate() actually got a live price/volume/spread check
    # and the candidate passed it. False means the info fetch failed/came
    # back empty (an MCP hiccup, not a real quality signal) - the candidate
    # is kept (same "don't cost a real candidate its slot on a hiccup"
    # intent as before) but demoted below every verified candidate for
    # quota/leftover-slot allocation, never the reverse. It still goes
    # through the SAME hard_vetoes.py price/volume/spread re-check every
    # candidate gets at scoring time before any order can be placed, so
    # "unverified" only ever costs it ranking priority, not a real safety
    # check.
    verified: bool = True


@dataclass
class ScreenerResult:
    candidates: list = field(default_factory=list)
    sources_used: list = field(default_factory=list)
    sources_failed: dict = field(default_factory=dict)  # source -> error str
    generated_at: str = ""
    quality_gate_dropped: int = 0  # how many candidates _apply_quality_gate() dropped this cycle
    # how many UNVERIFIED candidates made it into the final shortlist
    # (2026-07-21, external review) - MCP fetch failed for them, so they
    # were demoted below every verified candidate for slot allocation but
    # still filled leftover capacity. Distinct from quality_gate_dropped
    # (those never survived at all).
    quality_gate_unverified: int = 0


# Per-source defaults, used whenever config.yaml's screener.sources doesn't
# mention a key at all. config.yaml (see the screener: block added there)
# always wins when a source is present, even to just flip enabled on/off.
SCREENER_CONFIG = {
    "rs_gainers":        {"enabled": True,  "priority": 1, "min_pct_change": 3.0, "min_price": 5.0, "min_volume": 500_000},
    "volume_surge":      {"enabled": True,  "priority": 2, "min_pct_change": 1.0, "min_price": 5.0, "min_volume": 1_000_000},
    "momentum_screen":   {"enabled": False, "priority": 3},  # NOT IMPLEMENTED
    "finviz_screen":     {"enabled": True,  "priority": 4, "min_price": 5.0, "min_volume": 500_000},  # REAL (new-52w-high breakouts, see _screen_finviz)
    "sector_leaders":    {"enabled": True,  "priority": 5, "top_n_per_sector": 3},
    "pre_market_movers": {"enabled": True,  "priority": 0, "min_pct_change": 3.0, "min_price": 5.0, "min_volume": 300_000},
    "insider_buying":    {"enabled": False, "priority": 6},  # NOT IMPLEMENTED
    "gap_candidates":    {"enabled": True,  "priority": 1, "min_pct_change": 3.0, "min_price": 5.0, "min_volume": 500_000},
    # Full-market rotation sweep (2026-07-15g) - see _screen_universe_sweep.
    # batch_per_cycle bounds the per-cycle data cost; raise it (e.g. 25+)
    # once Alpaca keys are configured and bars are cheap.
    "universe_sweep":    {"enabled": True,  "priority": 3, "batch_per_cycle": 10},
    # Alpha Vantage top gainers/most-active (2026-07-15h) - one API call,
    # 4h-cached; a second vendor's independent mover ranking.
    "alpha_movers":      {"enabled": True,  "priority": 1, "min_price": 5.0, "min_volume": 500_000},
    # FMP biggest-gainers/most-actives (2026-07-15h) - third movers lens,
    # 2h-cached, 250-req/day free tier.
    "fmp_movers":        {"enabled": True,  "priority": 1, "min_price": 5.0, "min_volume": 500_000},
    # FinanceQuery day_gainers/most_actives (2026-07-16) - KEYLESS, quota-
    # free open-source Yahoo lens (see mcp_clients/market_data.py's
    # FinanceQueryProvider). Runs regardless of configured API keys, and
    # relieves the FMP/AV daily budgets.
    "fq_movers":         {"enabled": True,  "priority": 1, "min_price": 5.0, "min_volume": 500_000},
}

MAX_CANDIDATES_DEFAULT = 15

# Per-source slot quotas (deployment-review Priority: "allocate candidate
# quotas by screener type so no single source dominates"). Adapted from the
# review's own example table to this codebase's actual source names.
# Quotas are a SOFT allocation: if a source can't fill its quota, unused
# slots roll over to the highest-discovery_score candidates from any other
# source (see _allocate_by_quota()) rather than being wasted.
QUOTAS = {
    "rs_gainers": 4,
    "volume_surge": 2,
    "gap_candidates": 2,
    "pre_market_movers": 2,
    "sector_leaders": 2,
    # Full-market rotation (2026-07-15g): guaranteed shortlist slots for the
    # universe sweep so non-mover names actually reach scoring, not just the
    # quality gate.
    "universe_sweep": 4,
    "alpha_movers": 2,
    "fmp_movers": 2,
    "fq_movers": 2,
    # New-52w-high breakouts (2026-07-15c, finviz_screen) - a structural
    # signal, not a mover ranking, so a small guaranteed slot rather than
    # competing purely on discovery_score against %change-driven sources.
    "finviz_screen": 2,
}

# Regime-scaled candidate count (deployment-review: "in weak markets there
# simply aren't as many good opportunities"). Multiplies
# config.yaml's screener.max_candidates (the CHOPPY/normal baseline) rather
# than replacing it with a separate hardcoded table, so your own configured
# ceiling still means something in every regime.
REGIME_MULTIPLIER = {"BULL": 1.5, "CHOPPY": 1.0, "BEAR": 0.35, "CRISIS": 0.2}

# Discovery Score bucket weights (mirrors rules/swing_buy_rules.py's style:
# hardcoded, documented, sums to 1.0). Re-ranks EVERY raw candidate
# regardless of source on the SAME four dimensions, so a quiet multi-week
# outperformer can outrank a one-day spike.
_DISCOVERY_WEIGHTS = {"relative_strength": 0.40, "trend_alignment": 0.25,
                      "compression": 0.20, "source_signal": 0.15}


def run_screener(cfg: dict, mode: str = "swing", regime=None) -> ScreenerResult:
    """Main entry point, called from scheduler.py's run_cycle() only when
    config.yaml's screener.enabled is true. Returns a deduped, quota-
    allocated, Discovery-Score-ranked candidate list, capped at a regime-
    scaled version of screener.max_candidates. scheduler.py adds
    [c.ticker for c in result.candidates] onto that cycle's watchlist only -
    nothing is written back to config.yaml, so a candidate that doesn't fire
    a buy signal today simply isn't looked at again until the screener
    re-surfaces it."""
    scfg = cfg.get("screener", {}) or {}
    sources_cfg = scfg.get("sources", {}) or {}
    max_candidates = _max_candidates_for_regime(scfg, regime, risk_level=cfg.get("risk_level"))

    result = ScreenerResult(generated_at=datetime.now().isoformat())

    dispatch = {
        "rs_gainers": _screen_rs_gainers,
        "volume_surge": _screen_volume_surge,
        "momentum_screen": _screen_momentum_not_implemented,
        "finviz_screen": _screen_finviz,
        "sector_leaders": _screen_sector_leaders,
        "pre_market_movers": _screen_premarket_movers,
        "insider_buying": _screen_insider_not_implemented,
        "gap_candidates": _screen_gap_candidates,
        "universe_sweep": _screen_universe_sweep,
        "alpha_movers": _screen_alpha_movers,
        "fmp_movers": _screen_fmp_movers,
        "fq_movers": _screen_fq_movers,
    }

    # 2026-07-14: this loop used to call each source SEQUENTIALLY - real
    # cycle-timing evidence showed it was the single biggest chunk of an
    # entire scan cycle (~40s out of ~150s), more than either the quality
    # gate or the full per-ticker analysis loop, simply because 5 independent
    # MCP-backed source queries were run one after another instead of at
    # once. Each source is a read-only call with no shared state, so there's
    # no correctness reason for that - parallelize with one thread per
    # enabled source (at most 8, i.e. today's 5 real + 3 not-implemented
    # stubs that return instantly anyway).
    from concurrent.futures import ThreadPoolExecutor

    to_run = {}
    for source, fn in dispatch.items():
        defaults = SCREENER_CONFIG.get(source, {})
        source_cfg = sources_cfg.get(source, {}) or {}
        enabled = source_cfg.get("enabled", defaults.get("enabled", False))
        if not enabled:
            continue
        params = dict(defaults)
        params.update(source_cfg)
        to_run[source] = (fn, params)

    candidates_by_source = {}
    if to_run:
        with ThreadPoolExecutor(max_workers=min(8, len(to_run))) as ex:
            futures = {
                ex.submit(fn, params): source
                for source, (fn, params) in to_run.items()
            }
            for future in futures:
                source = futures[future]
                try:
                    candidates = future.result() or []
                    candidates_by_source[source] = candidates
                    if candidates:
                        result.sources_used.append(source)
                except Exception as e:
                    logger.error(f"Screener source '{source}' failed: {e}", exc_info=True)
                    result.sources_failed[source] = str(e)

    all_candidates = [c for cs in candidates_by_source.values() for c in cs]
    filtered = _pre_filter(all_candidates, cfg, mode)

    gate_cfg = scfg.get("quality_gate", {}) or {}
    if gate_cfg.get("enabled", True):
        before = len(filtered)
        filtered = _apply_quality_gate(filtered, cfg, mode)
        result.quality_gate_dropped = before - len(filtered)

    # Re-group the SURVIVING (post-filter) candidates by source for quota
    # allocation, and dedup within each group (a ticker hitting the same
    # source twice shouldn't happen, but be defensive).
    by_source: dict[str, list] = {}
    seen_per_source: dict[str, set] = {}
    for c in filtered:
        seen = seen_per_source.setdefault(c.source, set())
        if c.ticker in seen:
            continue
        seen.add(c.ticker)
        by_source.setdefault(c.source, []).append(c)

    # A ticker can legitimately appear under more than one source - keep
    # only its highest-priority (lowest .priority number) hit for scoring,
    # noting the others in .reason, same dedup spirit as v1.
    deduped = _dedup_keep_best(by_source)

    _score_candidates(deduped, mode, cfg=cfg)

    selected = _allocate_by_quota(deduped, QUOTAS, max_candidates)
    # Verified-first (2026-07-21, external review) - same _rank_key as the
    # quota allocation above, so an UNVERIFIED candidate that made it into
    # `selected` still doesn't display/queue ahead of a verified one.
    selected.sort(key=_rank_key)

    # Sector-diversity cap (2026-07-15, external review): a candidate-count
    # cap alone doesn't stop the whole shortlist being one AI/semis/energy
    # cluster exposed to a single sector move. No more than 30% of the
    # shortlist (min 3) may come from one sector; overflow names are
    # down-ranked out in discovery-score order. Unknown sectors are exempt
    # (can't punish a data gap - same UNKNOWN != FALSE principle as scoring).
    sector_cap = max(3, round(0.30 * max(len(selected), 1)))
    _by_sector = {}
    _diverse = []
    for c in selected:
        key = c.sector or "__unknown__"
        if key != "__unknown__" and _by_sector.get(key, 0) >= sector_cap:
            continue
        _by_sector[key] = _by_sector.get(key, 0) + 1
        _diverse.append(c)
    if len(_diverse) < len(selected):
        logger.info(f"Screener sector-diversity cap dropped "
                    f"{len(selected) - len(_diverse)} candidate(s) "
                    f"(max {sector_cap}/sector)")
    selected = _diverse
    result.quality_gate_unverified = sum(1 for c in selected if not c.verified)

    # Per-source contribution telemetry (2026-07-15f, review round 5): which
    # discovery sources actually fill the final shortlist - over time this
    # shows whether e.g. volume_surge earns its slots or is mostly noise.
    if selected:
        _src_counts = {}
        for c in selected:
            _src_counts[c.source] = _src_counts.get(c.source, 0) + 1
        logger.info(f"Screener shortlist by source: {dict(sorted(_src_counts.items(), key=lambda x: -x[1]))}")

    # Organic universe accumulation (2026-07-15g): every raw candidate any
    # source ever surfaces joins the persistent universe table, so the sweep
    # keeps widening even without Alpaca keys.
    try:
        from storage.database import Database as _UDB
        _UDB().upsert_universe_symbols([c.ticker for c in deduped], "organic")
    except Exception:
        pass

    # EXPLORATION / ROTATION slots (2026-07-15g - "how do we not miss the
    # stock that would have scored well?"): a pure top-N-by-discovery-score
    # shortlist re-examines the same leaders every cycle, so a name that's
    # never quite top-30 can stay invisible for weeks. A small quota is
    # reserved for structurally-valid survivors the engine has seen LEAST
    # recently (never-seen first, then oldest last_seen) - over days this
    # rotates the whole eligible universe through scoring without loosening
    # any quality bar (these names passed the same quality gate). The data
    # budget stays flat: the slots come out of max_candidates, not on top.
    exploration_slots = int(scfg.get("exploration_slots", 3))
    if exploration_slots > 0 and len(deduped) > len(selected):
        from storage.database import Database as _DB
        _xdb = _DB()
        chosen = {c.ticker for c in selected}
        pool = [c for c in deduped if c.ticker not in chosen]

        def _staleness_key(c):
            try:
                h = _xdb.get_screener_history(c.ticker, mode)
                # never-seen sorts first (empty string < any timestamp)
                return (h or {}).get("last_seen_at") or ""
            except Exception:
                return ""
        pool.sort(key=_staleness_key)
        explorers = pool[:exploration_slots]
        if explorers:
            # make room: drop the lowest-discovery-score incumbents
            selected = selected[:max(0, len(selected) - len(explorers))] + explorers
            for c in explorers:
                c.reason = (c.reason + " " if c.reason else "") + "[exploration slot]"
            logger.info(f"Screener exploration slots: {[c.ticker for c in explorers]} "
                        f"(least-recently-seen structurally-valid survivors)")

    result.candidates = selected
    return result


UNCAPPED_CANDIDATES = 10_000  # config.yaml's screener.max_candidates: 0 (or any
# value <= 0) means "no cap" - use a large-but-finite sentinel rather than
# float('inf') so it stays a valid list-slice bound in _allocate_by_quota()
# without special-casing. 10,000 is far beyond any realistic candidate
# universe this screener could ever produce in one cycle (dozens to low
# hundreds even across all sources), so it behaves as "unlimited" in
# practice. 2026-07-14 follow-up: Trinath asked to remove the cap after the
# quality gate (_apply_quality_gate()) was added - with real price/volume/
# spread filtering now happening BEFORE candidates reach this point, a
# survivor has already earned its slot, so there's no reason left to
# arbitrarily truncate the list down to a small top-N.


# Risk-level candidate scaling (2026-07-15, no-buys-round-2 audit): the
# pre-selection stage should behave like the risk level it feeds -
# CONSERVATIVE wants a few high-quality names getting full scoring
# attention, TURBO wants a wider funnel. Also directly reduces per-cycle
# MCP load at the cautious end.
RISK_LEVEL_MULTIPLIER = {"CONSERVATIVE": 0.6, "MODERATE": 0.8, "AGGRESSIVE": 1.0, "TURBO": 1.25}


def _max_candidates_for_regime(scfg: dict, regime, risk_level: str = None) -> int:
    base = scfg.get("max_candidates", MAX_CANDIDATES_DEFAULT)
    if base is None or base <= 0:
        return UNCAPPED_CANDIDATES
    mult = RISK_LEVEL_MULTIPLIER.get((risk_level or "").upper(), 1.0)
    if scfg.get("dynamic_by_regime", True) and regime is not None:
        dominant = getattr(regime, "dominant_regime", None)
        mult *= REGIME_MULTIPLIER.get(dominant, 1.0)
    return max(1, round(base * mult))


def _pre_filter(candidates: list, cfg: dict, mode: str) -> list:
    """Drop candidates already on the hand-curated watchlist (no point
    "discovering" what's already being watched), already holding an open
    position (db.get_open_position), or sitting in a post-exit re-entry
    cooldown (db.ticker_in_cooldown - the SAME cooldown swing_buy_rules.py's
    veto path already respects for the regular watchlist, applied here too
    so the screener can't immediately re-surface a ticker the position
    engine just exited).

    2026-07-14 follow-up (screener learning): also drops tickers that have
    PROVEN to be a bad use of a scan slot, learned from real scoring
    outcomes rather than discovery-time stats alone:
      - currently on an active stale/fallback-data streak (db.
        get_unhealthy_tickers() - see storage/database.py's
        record_ticker_data_health(), fed by rules/hard_vetoes.py's veto #16)
      - a proven track record of almost never qualifying, or chronically
        getting blocked by stale data, once actually scored (db.
        get_low_quality_screener_tickers() - fed by
        db.record_screener_outcome(), called from scheduler.py only for
        screener-sourced tickers). Both are config-gated under
        config.yaml's screener.learning section and both self-heal: a
        ticker's stats reset once it stops being discovered for
        stale_after_days (prune_stale_screener_candidates), so this isn't a
        permanent blocklist."""
    from storage.database import Database
    db = Database()

    learn_cfg = (cfg.get("screener", {}) or {}).get("learning", {}) or {}
    exclude_unhealthy = learn_cfg.get("exclude_unhealthy_tickers", True)
    unhealthy_min_consecutive = learn_cfg.get("unhealthy_min_consecutive", 3)
    exclude_low_quality = learn_cfg.get("exclude_low_quality_tickers", True)
    min_track_record = learn_cfg.get("min_track_record", 5)
    max_qualify_rate = learn_cfg.get("max_qualify_rate_to_exclude", 0.05)
    min_stale_block_rate = learn_cfg.get("min_stale_block_rate_to_exclude", 0.5)

    # 2026-07-14 fix: without a recency cutoff, a ticker excluded here can
    # never get re-evaluated to prove it's recovered - see
    # storage/database.py's get_unhealthy_tickers() docstring for the full
    # "screener catch-22" writeup. unhealthy_recheck_cooldown_minutes bounds
    # how long an exclusion lasts before the ticker gets one more shot.
    unhealthy_recheck_cooldown = learn_cfg.get("unhealthy_recheck_cooldown_minutes", 30)
    unhealthy_tickers = set()
    if exclude_unhealthy:
        try:
            unhealthy_tickers = {r["ticker"] for r in db.get_unhealthy_tickers(
                min_consecutive=unhealthy_min_consecutive, max_age_minutes=unhealthy_recheck_cooldown,
            )}
        except Exception as e:
            logger.warning(f"Screener: couldn't load unhealthy-ticker list: {e}")

    low_quality_tickers = set()
    if exclude_low_quality:
        try:
            low_quality_tickers = {
                r["ticker"] for r in db.get_low_quality_screener_tickers(
                    mode, min_track_record=min_track_record, max_qualify_rate=max_qualify_rate,
                    min_stale_block_rate=min_stale_block_rate,
                )
            }
        except Exception as e:
            logger.warning(f"Screener: couldn't load low-quality-ticker list: {e}")

    # Exclusion telemetry (2026-07-15e, review round 4): the learning filter
    # must not quietly become a permanent blacklist - log the current
    # exclusion-set sizes every cycle so growth is visible in the logs and
    # a bad regime's brandings can be spotted and reviewed.
    if unhealthy_tickers or low_quality_tickers:
        logger.info(
            f"Screener exclusion lists: {len(unhealthy_tickers)} unhealthy "
            f"(stale-data streak, self-heals after {unhealthy_recheck_cooldown} min), "
            f"{len(low_quality_tickers)} low-quality "
            f"(>= {min_track_record} scored cycles, qualify rate <= {max_qualify_rate:.0%}): "
            f"{sorted(low_quality_tickers)[:10]}")

    watchlist = {t.upper() for t in cfg.get("watchlist", [])}
    out = []
    dropped_unhealthy, dropped_low_quality = 0, 0
    for c in candidates:
        c.ticker = c.ticker.upper()
        if c.ticker in watchlist:
            continue
        try:
            if db.get_open_position(c.ticker):
                continue
        except Exception:
            pass
        try:
            if db.ticker_in_cooldown(c.ticker):
                continue
        except Exception:
            pass
        if c.ticker in unhealthy_tickers:
            dropped_unhealthy += 1
            continue
        if c.ticker in low_quality_tickers:
            dropped_low_quality += 1
            continue
        out.append(c)

    if dropped_unhealthy or dropped_low_quality:
        logger.info(f"Screener: excluded {dropped_unhealthy} candidate(s) with active stale-data streaks, "
                    f"{dropped_low_quality} with a proven low-quality track record")
    return out


def _apply_quality_gate(candidates: list, cfg: dict, mode: str) -> list:
    """2026-07-14 follow-up (Trinath: "not even a single stock being scored...
    apply metrics even before selecting"). Root cause, confirmed against real
    production signals: _pre_filter() above only ever checked IDENTITY
    (watchlist/open-position/cooldown) and PAST outcome history (active
    stale-data streak / proven low qualify rate) - nothing checked whether a
    freshly-discovered candidate could even survive rules/hard_vetoes.py's
    price/volume/spread gates BEFORE it consumed a scan slot. The per-source
    SCREENER_CONFIG min_price/min_volume knobs look like they'd cover this,
    but they don't check the same fields the hard veto does: they filter on
    TODAY's day-volume and discovery-time price from the raw screener API
    response, not averageVolume/live bid-ask - a candidate can clear those
    and still be doomed. Measured impact before this fix: 167/185 signals in
    one session were vetoed before scoring ever ran, 118 of those on
    SPREAD_WIDE alone (see rules/spread_quality.py's companion fix for the
    data-quality half of that specific number).

    This does ONE lightweight yfinance_get_ticker_info call per surviving
    candidate - NOT the full 7-call get_all() bundle a scoring pass would
    trigger - parallelized the same way scheduler.py's per-ticker loop is,
    and checks the EXACT price/volume/spread fields+thresholds
    rules/hard_vetoes.py enforces at scoring time. Net MCP-call effect is a
    wash at worst, usually a net reduction: every candidate dropped here is
    one fewer full analyze()+indicator-calc pass wasted on a candidate that
    was going to be vetoed anyway. Config-gated (screener.quality_gate.
    enabled, default True) in case Trinath ever wants the old
    identity-only-filtering behavior back."""
    from concurrent.futures import ThreadPoolExecutor
    from mcp_clients.yfinance_mcp import YFinanceMCP
    from rules.spread_quality import evaluate as evaluate_spread

    if not candidates:
        return candidates

    gate_cfg = (cfg.get("screener", {}) or {}).get("quality_gate", {}) or {}
    min_price = gate_cfg.get("min_price", 10.0)
    max_price = gate_cfg.get("max_price", 1000.0)
    # Config value is the SWING baseline; DAY mode enforces the same 2M
    # floor rules/hard_vetoes.py will apply at scoring time regardless of
    # what config says - otherwise day-mode candidates pass the gate only
    # to be LOW_VOLUME-vetoed a minute later (2026-07-15).
    min_avg_volume = gate_cfg.get("min_avg_volume", 1_000_000)
    if mode == "day":
        min_avg_volume = max(min_avg_volume, 2_000_000)
    max_parallel = min(gate_cfg.get("max_parallel", 12), len(candidates))

    yf = YFinanceMCP()

    def _check(c):
        try:
            info = yf.get_ticker_info(c.ticker) or {}
        except Exception as e:
            logger.warning(f"Quality gate: {c.ticker} info fetch failed: {e}")
            # UNVERIFIED, not a free pass (2026-07-21, external review): an
            # MCP hiccup shouldn't cost a real candidate its slot outright,
            # but it also shouldn't get the same priority-ranking credit as
            # a candidate that actually cleared a live check - see
            # ScreenerCandidate.verified's field comment.
            c.verified = False
            return c, True, None
        if not info:
            c.verified = False
            return c, True, None

        price = info.get("regularMarketPrice") or info.get("currentPrice") or 0.0
        avg_vol = info.get("averageVolume") or 0
        bid = info.get("bid") or price
        ask = info.get("ask") or price

        if price < min_price or price > max_price:
            return c, False, f"price ${price:.2f} outside ${min_price:.0f}-${max_price:.0f}"
        if avg_vol < min_avg_volume:
            return c, False, f"avg volume {avg_vol:,.0f} < {min_avg_volume:,}"

        spread_result = evaluate_spread({"bid": bid, "ask": ask, "price": price}, mode=mode)
        if spread_result.hard_veto:
            return c, False, spread_result.reason

        c.price = price  # opportunistic refresh - discovery-time price can be stale by the time we get here
        c.volume = avg_vol
        c.sector = info.get("sector") or None  # for the sector-diversity cap (2026-07-15)
        return c, True, None

    survivors = []
    drop_reasons = {}
    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        for c, kept, reason in ex.map(_check, candidates):
            if kept:
                survivors.append(c)
            else:
                key = reason.split(" ", 1)[0] if reason else "unknown"
                drop_reasons[key] = drop_reasons.get(key, 0) + 1

    unverified_count = sum(1 for c in survivors if not c.verified)
    if len(survivors) != len(candidates) or unverified_count:
        logger.info(f"Screener quality gate: {len(candidates)} candidates -> {len(survivors)} survived "
                    f"({unverified_count} UNVERIFIED - MCP fetch failed, demoted below verified "
                    f"candidates for slot allocation, not dropped) (dropped: {dict(drop_reasons)})")
    return survivors


def _dedup_keep_best(by_source: dict) -> dict:
    """Cross-source dedup: if a ticker shows up under more than one source,
    keep it only in its highest-priority (lowest .priority) source's list,
    noting the other source(s) in .reason. Returns a NEW by_source dict."""
    best_source_for_ticker = {}
    for source, cands in by_source.items():
        for c in cands:
            existing = best_source_for_ticker.get(c.ticker)
            # Prefer a VERIFIED hit over an unverified one regardless of
            # source priority (2026-07-21, external review) - the same
            # ticker can surface from two sources with one quality-gate
            # check succeeding and the other MCP-failing; keeping the
            # unverified copy just because its source has a lower priority
            # number would throw away a real check for no reason.
            if existing is None:
                best_source_for_ticker[c.ticker] = c
            elif c.verified and not existing.verified:
                best_source_for_ticker[c.ticker] = c
            elif c.verified == existing.verified and c.priority < existing.priority:
                best_source_for_ticker[c.ticker] = c

    out: dict[str, list] = {}
    for ticker, c in best_source_for_ticker.items():
        out.setdefault(c.source, []).append(c)
    return out


def _rank_key(c):
    """Shared sort key for every slot-allocation ranking step below
    (2026-07-21, external review - "do not grant priority ranking credit"
    to an UNVERIFIED candidate). Verified candidates (0) always sort ahead
    of unverified ones (1) regardless of discovery_score; within the same
    verified status, higher discovery_score wins as before. This is the
    ONLY change from the pre-review `-c.discovery_score` key - an unverified
    candidate can still win a slot, but only once every verified candidate
    competing for the same quota/leftover/overflow pool has already been
    placed."""
    return (0 if getattr(c, "verified", True) else 1, -c.discovery_score)


def _allocate_by_quota(by_source: dict, quotas: dict, max_candidates: int) -> list:
    """Fixed slot budget per source, leftover slots rolled over to whichever
    OTHER candidates scored highest (deployment-review: "allocate candidate
    quotas by screener type so no single source dominates"). Quotas are
    scaled down proportionally if their sum exceeds max_candidates (e.g. a
    BEAR-regime-scaled max_candidates of 5 shouldn't try to honor a 12-slot
    quota table). Verified candidates are ranked ahead of UNVERIFIED ones at
    every step (_rank_key) - see ScreenerCandidate.verified's field comment."""
    quota_total = sum(quotas.get(s, 0) for s in by_source) or 1
    scale = min(1.0, max_candidates / quota_total) if quota_total > max_candidates else 1.0

    selected = []
    leftover_pool = []
    for source, cands in by_source.items():
        cands_sorted = sorted(cands, key=_rank_key)
        quota = max(0, round(quotas.get(source, 0) * scale)) if source in quotas else 0
        selected.extend(cands_sorted[:quota])
        leftover_pool.extend(cands_sorted[quota:])

    leftover_pool.sort(key=_rank_key)
    slots_left = max_candidates - len(selected)
    if slots_left > 0:
        selected.extend(leftover_pool[:slots_left])
    elif len(selected) > max_candidates:
        selected.sort(key=_rank_key)
        selected = selected[:max_candidates]
    return selected


# ---------------------------------------------------------------------
# Discovery Score - real relative-strength/trend/compression re-ranking,
# applied to every surviving candidate regardless of source.
# ---------------------------------------------------------------------

def _score_candidates(by_source: dict, mode: str, cfg: dict = None):
    """Mutates each candidate's .discovery_score, .rs_*, .trend_aligned,
    .persistence_bonus in place. Fetches each candidate's (and SPY's) daily
    OHLCV once, in parallel, then does plain-Python rolling-window math -
    see module docstring for why this doesn't reuse
    engine/ticker_analyzer.py's pandas_ta-based calc.

    2026-07-14: max_workers used to be a hardcoded min(20, len(tickers)) -
    lowered to a config-driven default of 8 (screener.discovery_max_parallel)
    after real production evidence (a cycle stuck 23+ min, log showing
    "unhandled errors in a TaskGroup" from concurrent uvx subprocess spawns)
    showed 20 concurrent MCP subprocess spawns is not reliably safe,
    especially now that screener.max_candidates can be uncapped (many more
    raw candidates reaching this function than the old top-20-ish design
    ever produced)."""
    from concurrent.futures import ThreadPoolExecutor
    from storage.database import Database

    all_candidates = [c for cs in by_source.values() for c in cs]
    if not all_candidates:
        return

    spy_bars = _fetch_bars("SPY")

    max_parallel = ((cfg or {}).get("screener", {}) or {}).get("discovery_max_parallel", 8)
    tickers = [c.ticker for c in all_candidates]
    bars_by_ticker = {}
    with ThreadPoolExecutor(max_workers=min(max_parallel, len(tickers))) as ex:
        for ticker, bars in ex.map(lambda t: (t, _fetch_bars(t)), tickers):
            if bars:
                bars_by_ticker[ticker] = bars

    db = Database()
    try:
        db.prune_stale_screener_candidates(mode)
    except Exception as e:
        logger.warning(f"Screener: couldn't prune stale candidates: {e}")

    for c in all_candidates:
        bars = bars_by_ticker.get(c.ticker)
        score, inputs = _discovery_score(c, bars, spy_bars)
        c.discovery_score = score
        c.rs_20d = inputs.get("rs_20d")
        c.rs_50d = inputs.get("rs_50d")
        c.rs_100d = inputs.get("rs_100d")
        c.trend_aligned = inputs.get("trend_aligned", False)

        try:
            history = db.get_screener_history(c.ticker, mode)
            c.persistence_bonus = _persistence_bonus(history, score)
            c.discovery_score = max(0.0, min(100.0, c.discovery_score + c.persistence_bonus))
            db.upsert_screener_candidate(
                c.ticker, mode, score, source=c.source,
                decomposition={
                    "rs_20d": c.rs_20d, "rs_50d": c.rs_50d, "rs_100d": c.rs_100d,
                    "trend_aligned": bool(c.trend_aligned),
                    "persistence_bonus": round(c.persistence_bonus, 1),
                    "final_discovery_score": round(c.discovery_score, 1),
                })
        except Exception as e:
            logger.warning(f"Screener: persistence tracking failed for {c.ticker}: {e}")


MIN_TRACK_RECORD_FOR_OUTCOME_BONUS = 5  # cycles scored before trusting real outcome stats over raw appearance count
FULL_BONUS_QUALIFY_RATE = 0.25          # a ticker qualifying 1-in-4 times it's scored earns the full +10 bonus
STALE_BLOCK_PENALTY = -15.0             # chronically bad data actively costs a candidate slot, not just no bonus
# 2026-07-14: Trinath asked whether the screener's learning also improves at
# picking candidates that go on to score WELL, not just candidates that
# happen to clear the qualify bar - explicitly flagging he didn't want an
# "overkill" addition that outgrows the screener's actual job (finding
# candidates worth a scoring pass, not replacing the scoring engine).
# storage/database.py's record_screener_outcome() has been writing
# sum_buy_pct/n_buy_pct_samples (the real score, including sub-threshold
# HOLDs - not just a qualified/not flag) every scored cycle since the
# original screener-learning pass, but _persistence_bonus() never read them.
# FULL_BONUS_AVG_SCORE_PCT is the average-score bar for full secondary
# credit - deliberately set BELOW the live buy threshold (55% at
# AGGRESSIVE), since a candidate consistently scoring 55-65% even without
# qualifying yet is meaningfully more promising than one flatlining at 20%.
FULL_BONUS_AVG_SCORE_PCT = 60.0


def _persistence_bonus(history: dict, current_score: float) -> float:
    """2026-07-14 follow-up (screener learning): previously this only
    rewarded raw REAPPEARANCE (+2/appearance, capped +10) regardless of
    whether the candidate ever actually qualified once scored - a real
    feedback loop where the same tickers kept winning quota slots purely
    for showing up again, whether or not they were ever any good (or even
    had usable data). Now outcome-aware, using db.record_screener_outcome()
    stats (n_scored/n_qualified/n_stale_data_blocked, only populated for
    screener-sourced tickers that scheduler.py actually ran through
    scoring):

      - Fewer than MIN_TRACK_RECORD_FOR_OUTCOME_BONUS scored cycles: no
        outcome data to judge by yet, falls back to the original
        appearance-based bonus (a sustained/rising discovery score still
        earns a small boost before there's anything else to go on).
      - Enough track record, but chronically blocked by stale/fallback data
        (>=50% of scored cycles): ACTIVELY PENALIZED, not just zeroed - this
        candidate is costing a scan slot for no reason (most such tickers
        should already be excluded earlier by _pre_filter's
        get_low_quality_screener_tickers() check; this is the safety net
        for one that hasn't crossed that threshold yet).
      - Enough track record, real qualify rate available: bonus is a
        weighted blend - 70% from qualify_rate (did it actually convert to
        a real BUY - the ground-truth outcome, kept as the dominant term on
        purpose) and 30% from how consistently STRONG its scores run even
        when they don't convert (sum_buy_pct/n_buy_pct_samples vs.
        FULL_BONUS_AVG_SCORE_PCT). Same +10 ceiling as before this
        follow-up - deliberately NOT a new, bigger reward budget, just a
        smarter use of data already being collected within the existing
        envelope, so this can't grow into something that quietly overrides
        the scoring engine's own job."""
    if not history:
        return 0.0

    n_scored = history.get("n_scored", 0) or 0
    if n_scored < MIN_TRACK_RECORD_FOR_OUTCOME_BONUS:
        times_seen = history.get("times_seen", 0) or 0
        best_score = history.get("best_score") or 0.0
        if best_score <= 0 or current_score < best_score * 0.9:
            return 0.0
        return min(10.0, max(0, times_seen - 1) * 2.0)

    n_stale = history.get("n_stale_data_blocked", 0) or 0
    if (n_stale / n_scored) >= 0.5:
        return STALE_BLOCK_PENALTY

    n_qualified = history.get("n_qualified", 0) or 0
    qualify_rate = n_qualified / n_scored
    qualify_term = 10.0 * min(1.0, qualify_rate / FULL_BONUS_QUALIFY_RATE)

    n_pct_samples = history.get("n_buy_pct_samples", 0) or 0
    if n_pct_samples > 0:
        avg_buy_pct = (history.get("sum_buy_pct", 0.0) or 0.0) / n_pct_samples
        score_term = 10.0 * min(1.0, max(0.0, avg_buy_pct) / FULL_BONUS_AVG_SCORE_PCT)
        bonus = 0.7 * qualify_term + 0.3 * score_term
    else:
        # No scored-pct samples yet (e.g. every past cycle was hard-vetoed
        # before scoring ran) - fall back to the pure qualify-rate term,
        # same as before this follow-up.
        bonus = qualify_term

    return round(min(10.0, bonus), 1)


def _discovery_score(c, bars, spy_bars) -> tuple:
    """Returns (score 0-100, inputs dict). Any component whose data isn't
    available (e.g. bars fetch failed) contributes a neutral 50/100 for that
    component rather than crashing or zeroing the whole score - a screener
    ranking pass should degrade gracefully, not drop a candidate just
    because one auxiliary fetch failed."""
    inputs = {}

    rs_points = 50.0
    if bars and spy_bars:
        rs_20d = _relative_return(bars["closes"], spy_bars["closes"], 20)
        rs_50d = _relative_return(bars["closes"], spy_bars["closes"], 50)
        rs_100d = _relative_return(bars["closes"], spy_bars["closes"], 100)
        inputs["rs_20d"], inputs["rs_50d"], inputs["rs_100d"] = rs_20d, rs_50d, rs_100d
        rs_values = [v for v in (rs_20d, rs_50d, rs_100d) if v is not None]
        if rs_values:
            rs_avg = sum(rs_values) / len(rs_values)
            # -10pp -> 0, +40pp -> 100, linear, clamped
            rs_points = max(0.0, min(100.0, (rs_avg + 10.0) / 50.0 * 100.0))

    trend_points = 50.0
    if bars and len(bars["closes"]) >= 200:
        price = bars["closes"][-1]
        sma20 = _sma(bars["closes"], 20)
        sma50 = _sma(bars["closes"], 50)
        sma200 = _sma(bars["closes"], 200)
        conditions = [price > sma20, sma20 > sma50, sma50 > sma200] if (sma20 and sma50 and sma200) else []
        if conditions:
            trend_points = sum(conditions) / len(conditions) * 100.0
            inputs["trend_aligned"] = all(conditions)

    compression_points = 0.0
    if bars and len(bars["highs"]) >= 20:
        squeeze_active, is_nr7, is_nr4, is_inside_day = _range_compression_signals(
            bars["highs"], bars["lows"], bars["closes"])
        pts = 0
        if squeeze_active: pts += 6
        if is_nr7: pts += 6
        elif is_nr4: pts += 3
        if is_inside_day: pts += 2
        compression_points = min(100.0, pts / 14.0 * 100.0)

    # source_signal: today's %change is still evidence, just capped at 15%
    # of the total instead of being the whole story.
    pct = c.pct_change or 0.0
    source_points = max(0.0, min(100.0, pct / 20.0 * 100.0))

    score = (rs_points * _DISCOVERY_WEIGHTS["relative_strength"] +
             trend_points * _DISCOVERY_WEIGHTS["trend_alignment"] +
             compression_points * _DISCOVERY_WEIGHTS["compression"] +
             source_points * _DISCOVERY_WEIGHTS["source_signal"])
    return max(0.0, min(100.0, score)), inputs


def _fetch_bars(ticker: str) -> dict:
    """1y daily OHLCV (enough for a real SMA200 + 100d relative strength),
    cached for TTL_BARS since these are daily bars that don't need
    re-fetching every scan cycle. Returns {} on any failure - callers treat
    that as "no data for this component" (see _discovery_score's neutral-
    default handling), not a hard error."""
    cache_key = f"screener_bars_{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        from mcp_clients.yfinance_mcp import YFinanceMCP
        yf = YFinanceMCP()
        raw = yf.get_price_history(ticker, period="1y", interval="1d")
        rows = _rows(raw)
        closes, highs, lows = [], [], []
        for row in rows:
            if not isinstance(row, dict):
                continue
            c = _to_float(_field(row, "close", "Close", "c"))
            h = _to_float(_field(row, "high", "High", "h"))
            l = _to_float(_field(row, "low", "Low", "l"))
            if c is not None:
                closes.append(c)
                highs.append(h if h is not None else c)
                lows.append(l if l is not None else c)
        bars = {"closes": closes, "highs": highs, "lows": lows} if len(closes) >= 20 else {}
        cache.set(cache_key, bars, TTL_BARS)
        return bars
    except Exception as e:
        logger.warning(f"Screener: bars fetch failed for {ticker}: {e}")
        return {}


# ---------------------------------------------------------------------
# Plain-Python rolling-window math (no pandas_ta) - see module docstring for
# why this is a deliberately-separate implementation from
# engine/ticker_analyzer.py's, used only to RANK screener candidates.
# ---------------------------------------------------------------------

def _sma(closes: list, period: int):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _relative_return(closes: list, spy_closes: list, n: int):
    """Candidate's n-day return minus SPY's n-day return, in percentage
    points - REAL relative strength, not just absolute momentum."""
    if len(closes) <= n or len(spy_closes) <= n:
        return None
    own = (closes[-1] - closes[-1 - n]) / closes[-1 - n] * 100 if closes[-1 - n] else None
    spy = (spy_closes[-1] - spy_closes[-1 - n]) / spy_closes[-1 - n] * 100 if spy_closes[-1 - n] else None
    if own is None or spy is None:
        return None
    return round(own - spy, 2)


def _atr(highs: list, lows: list, closes: list, period: int = 14):
    """Simple-moving-average True Range - a standard ATR variant, NOT
    identical to engine/ticker_analyzer.py's Wilder-smoothed pandas_ta ATR
    (ATRr_14). Fine for a ranking-only pass; not used anywhere scoring-
    critical."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _bollinger(closes: list, period: int = 20, k: float = 2.0):
    if len(closes) < period:
        return None, None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    std = var ** 0.5
    return mean + k * std, mean - k * std


def _range_compression_signals(highs: list, lows: list, closes: list) -> tuple:
    """Same TTM-Squeeze/NR7/NR4/inside-day definitions as
    engine/ticker_analyzer.py's _calc_volatility_compression, reimplemented
    with plain rolling windows over the bars this module already fetched -
    see module docstring for why this is a separate implementation."""
    squeeze_active = is_nr7 = is_nr4 = is_inside_day = False

    sma20 = _sma(closes, 20)
    atr14 = _atr(highs, lows, closes, 14)
    bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
    if sma20 and atr14 and bb_upper and len(closes) >= 26:
        kc_upper = sma20 + 1.5 * atr14
        kc_lower = sma20 - 1.5 * atr14
        # Recompute the squeeze boolean over the trailing 6 bars to check
        # "was compressed in the last 5, released now" - approximated here
        # using the SAME (final) SMA20/ATR14 for all 6 bars rather than a
        # true rolling recalculation at each bar, since that's a much
        # cheaper approximation adequate for ranking purposes.
        compressed_now = bb_upper < kc_upper and bb_lower > kc_lower
        # Approximate "was compressed in recent bars" via a wider band check
        # 5 bars back using the same bollinger/keltner formulas.
        if len(closes) >= 25:
            bb_u5, bb_l5 = _bollinger(closes[:-5], 20, 2.0)
            sma20_5 = _sma(closes[:-5], 20)
            atr14_5 = _atr(highs[:-5], lows[:-5], closes[:-5], 14)
            was_compressed = bool(bb_u5 and sma20_5 and atr14_5 and
                                   bb_u5 < sma20_5 + 1.5 * atr14_5 and
                                   bb_l5 > sma20_5 - 1.5 * atr14_5)
            squeeze_active = was_compressed and not compressed_now

    if len(closes) >= 7:
        ranges7 = [highs[i] - lows[i] for i in range(len(closes) - 7, len(closes))]
        is_nr7 = ranges7[-1] == min(ranges7)
    if len(closes) >= 4:
        ranges4 = [highs[i] - lows[i] for i in range(len(closes) - 4, len(closes))]
        is_nr4 = ranges4[-1] == min(ranges4)
    if len(closes) >= 2:
        is_inside_day = highs[-1] <= highs[-2] and lows[-1] >= lows[-2]

    return squeeze_active, is_nr7, is_nr4, is_inside_day


# ---------------------------------------------------------------------
# Shared row-parsing helpers for the REAL (yfmcp-backed) sources
# ---------------------------------------------------------------------

def _rows(raw) -> list:
    """Defensive multi-key unwrap for MCP screener responses - same pattern
    engine/market_breadth.py's _closes() uses, since the exact top-level key
    yfmcp's screener/price-history tools wrap rows in hasn't been verified
    against live output (see mcp_clients/yfinance_mcp.py's screen_equity/
    screen_gappers/get_top_in_sector/get_price_history docstrings)."""
    if isinstance(raw, dict):
        for key in ("quotes", "data", "results", "rows", "history", "prices"):
            if isinstance(raw.get(key), list):
                return raw[key]
        return []
    if isinstance(raw, list):
        return raw
    return []


def _field(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _row_to_candidate(row: dict, source: str, priority: int):
    if not isinstance(row, dict):
        return None
    ticker = _field(row, "symbol", "ticker", "Symbol", "Ticker")
    if not ticker:
        return None
    pct = _to_float(_field(row, "percentchange", "regularMarketChangePercent", "pct_change", "changePercent"))
    vol = _to_float(_field(row, "dayvolume", "regularMarketVolume", "volume", "Volume"))
    price = _to_float(_field(row, "intradayprice", "regularMarketPrice", "price", "Price"))
    return ScreenerCandidate(
        ticker=str(ticker).upper(), source=source, priority=priority,
        pct_change=pct, volume=vol, price=price,
        reason=f"{source}: {pct:+.1f}%" if pct is not None else source,
    )


def _candidates_from_rows(raw, source: str, priority: int) -> list:
    return [c for c in (_row_to_candidate(r, source, priority) for r in _rows(raw)) if c]


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()  # best-effort fallback if tzdata is unavailable


# ---------------------------------------------------------------------
# REAL sources
# ---------------------------------------------------------------------

def _screen_rs_gainers(params: dict) -> list:
    """Relative-strength gainers - custom equity screen sorted by %change
    descending. REAL (yfinance_screen). NOTE: despite the name (kept for
    backward config-compatibility), this is TODAY's %change, not a
    multi-week relative-strength screen - the real 20/50/100-day relative
    strength now happens in the Discovery Score re-ranking pass
    (_discovery_score()) applied to every candidate from every source, since
    yfmcp's screener query language doesn't expose a "N-day return" field to
    query the whole market by."""
    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    cache_key = f"screener_rs_gainers_{params.get('min_pct_change')}_{params.get('min_volume')}"
    raw = cache.get(cache_key)
    if raw is None:
        raw = yf.screen_equity(
            min_percent_change=params.get("min_pct_change", 3.0),
            min_price=params.get("min_price", 5.0),
            min_volume=params.get("min_volume", 500_000),
            sort_field="percentchange",
            size=50,
        )
        cache.set(cache_key, raw, TTL_SCREENER)
    return _candidates_from_rows(raw, "rs_gainers", params.get("priority", 1))


def _screen_volume_surge(params: dict) -> list:
    """Same custom equity screen, sorted by day volume instead of %change -
    surfaces unusual volume even on names that haven't moved much yet.
    REAL (yfinance_screen)."""
    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    cache_key = f"screener_volume_surge_{params.get('min_pct_change')}_{params.get('min_volume')}"
    raw = cache.get(cache_key)
    if raw is None:
        raw = yf.screen_equity(
            min_percent_change=params.get("min_pct_change", 1.0),
            min_price=params.get("min_price", 5.0),
            min_volume=params.get("min_volume", 1_000_000),
            sort_field="dayvolume",
            size=50,
        )
        cache.set(cache_key, raw, TTL_SCREENER)
    return _candidates_from_rows(raw, "volume_surge", params.get("priority", 2))


def _screen_gap_candidates(params: dict) -> list:
    """Purpose-built gap screener. REAL (yfinance_screen_gappers)."""
    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    cache_key = "screener_gap_candidates"
    raw = cache.get(cache_key)
    if raw is None:
        raw = yf.screen_gappers(
            min_percent_change=params.get("min_pct_change", 3.0),
            min_price=params.get("min_price", 5.0),
            min_volume=params.get("min_volume", 500_000),
            size=50,
        )
        cache.set(cache_key, raw, TTL_SCREENER)
    return _candidates_from_rows(raw, "gap_candidates", params.get("priority", 1))


def _screen_premarket_movers(params: dict) -> list:
    """Same underlying gap-scan call as gap_candidates, but only actually
    runs (and is only meaningfully labeled "pre-market") during the
    pre-market window (~4:00-9:30am ET). Outside that window this returns []
    rather than relabeling regular-session gap data as pre-market. REAL
    (yfinance_screen_gappers), time-gated - not cached, since re-fetching a
    handful of times across a ~5.5hr window is cheap and freshness matters
    more here than in the other sources."""
    now_et = _now_et()
    if not (dtime(4, 0) <= now_et.time() < dtime(9, 30)):
        return []
    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    raw = yf.screen_gappers(
        min_percent_change=params.get("min_pct_change", 3.0),
        min_price=params.get("min_price", 5.0),
        min_volume=params.get("min_volume", 300_000),
        size=50,
    )
    return _candidates_from_rows(raw, "pre_market_movers", params.get("priority", 0))


def _screen_finviz(params: dict) -> list:
    """New-52-week-high breakouts, real (finviz.com via mcp_clients/
    finviz_screen.py). 2026-07-15c: this was NOT IMPLEMENTED because a live
    test of finviz.Screener's output came back column-misaligned; that
    parsing bug is now fixed (see finviz_screen.py's _patched_get_table()
    docstring), so this source is real. Deliberately a DIFFERENT discovery
    lens than rs_gainers/volume_surge/gap_candidates (all same-day
    %change/volume, yfinance-backed): new-high is a price-STRUCTURE signal
    none of the yfinance-backed sources can query for directly."""
    from mcp_clients.finviz_screen import get_new_highs
    cache_key = f"screener_finviz_newhigh_{params.get('min_price')}_{params.get('min_volume')}"
    raw = cache.get(cache_key)
    if raw is None:
        raw = get_new_highs(
            min_price=params.get("min_price", 5.0),
            min_volume=params.get("min_volume", 500_000),
            limit=50,
        )
        cache.set(cache_key, raw, TTL_SCREENER)
    return _candidates_from_rows(raw, "finviz_screen", params.get("priority", 4))


def _sector_rotation_weights(top_n_base: int) -> dict:
    """Ranks the 11 SPDR sectors by their own real 1-month sector-vs-SPY
    relative strength (engine/market_breadth.get_sector_return, already
    computed for the breadth proxy - zero extra MCP calls) and scans the
    strongest few harder than the weakest (deployment-review: "instead of
    top companies, find top industries, then top stocks within them").
    Top 3 sectors get top_n_base+2, next 4 get top_n_base, bottom 4 get
    max(1, top_n_base-2)."""
    from engine.market_breadth import get_sector_return
    ranked = []
    for etf, name in SECTOR_ETF_NAMES.items():
        try:
            rs = get_sector_return(name)
            ranked.append((name, rs.get("return_1m", 0.0)))
        except Exception:
            ranked.append((name, 0.0))
    ranked.sort(key=lambda kv: kv[1], reverse=True)

    weights = {}
    for i, (name, _) in enumerate(ranked):
        if i < 3:
            weights[name] = top_n_base + 2
        elif i < 7:
            weights[name] = top_n_base
        else:
            weights[name] = max(1, top_n_base - 2)
    return weights


def _screen_sector_leaders(params: dict) -> list:
    """Top-performing companies in each of the 11 SPDR sectors, scanned
    harder in the strongest sectors (see _sector_rotation_weights()). REAL
    (yfinance_get_top)."""
    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    top_n_base = params.get("top_n_per_sector", 3)
    cache_key = f"screener_sector_leaders_{top_n_base}"
    candidates = cache.get(cache_key)
    if candidates is None:
        try:
            weights = _sector_rotation_weights(top_n_base)
        except Exception as e:
            logger.warning(f"sector_leaders: rotation ranking failed, falling back to flat top_n: {e}")
            weights = {}
        candidates = []
        for etf, sector_name in SECTOR_ETF_NAMES.items():
            top_n = weights.get(sector_name, top_n_base)
            try:
                raw = yf.get_top_in_sector(sector_name, top_type="top_performing_companies", top_n=top_n)
            except Exception as e:
                logger.warning(f"sector_leaders: {sector_name} failed: {e}")
                continue
            for c in _candidates_from_rows(raw, "sector_leaders", params.get("priority", 5)):
                c.reason = f"sector_leaders: top {sector_name} (rotation weight {top_n})"
                candidates.append(c)
        cache.set(cache_key, candidates, TTL_SCREENER)
    return candidates


# ---------------------------------------------------------------------
# NOT IMPLEMENTED sources - no verified market-wide screening tool exists
# on these MCP server wrappers (see module docstring). Logged once per
# process (not once per cycle) so they don't spam the log every
# scan_interval_minutes.
# ---------------------------------------------------------------------
_warned = set()


def _warn_once(source: str, reason: str):
    if source not in _warned:
        logger.warning(f"Screener source '{source}' is not implemented: {reason}")
        _warned.add(source)


def _screen_alpha_movers(params: dict) -> list:
    """Alpha Vantage TOP_GAINERS_LOSERS (2026-07-15h) - one API call yields
    ~40 usable candidates (top gainers + most active), cached 4h so the free
    25-req/day quota lasts. A genuinely different lens from the yfinance
    screens: AV's most-active list often surfaces names yfmcp's screens
    miss. Enabled automatically when ALPHAVANTAGE_API_KEY is set."""
    from mcp_clients.market_data import router as md_router
    if not md_router.alphavantage.key:
        return []
    movers = md_router.alphavantage.get_top_movers()
    if not movers:
        return []
    min_price = params.get("min_price", 5.0)
    min_volume = params.get("min_volume", 500_000)
    out = []
    for group, rows in (("gainer", movers.get("gainers", [])),
                        ("most_active", movers.get("most_active", []))):
        for r in rows:
            if r["price"] >= min_price and r["volume"] >= min_volume:
                out.append(ScreenerCandidate(
                    ticker=r["ticker"], source="alpha_movers",
                    reason=f"AV {group} {r['change_pct']:+.1f}%",
                    pct_change=r["change_pct"], volume=r["volume"],
                    price=r["price"], priority=2))
    return out


def _screen_fmp_movers(params: dict) -> list:
    """Financial Modeling Prep biggest-gainers + most-actives (2026-07-15h) -
    a third independent movers lens (2 calls, cached 2h, 250-req/day free
    tier with a 200/day self-cap). Active only when FMP_API_KEY is set."""
    from mcp_clients.market_data import router as md_router
    if not md_router.fmp.key:
        return []
    movers = md_router.fmp.get_movers()
    if not movers:
        return []
    min_price = params.get("min_price", 5.0)
    min_volume = params.get("min_volume", 500_000)
    out = []
    for group, rows in (("gainer", movers.get("gainers", [])),
                        ("most_active", movers.get("most_active", []))):
        for r in rows:
            if r["price"] >= min_price and (r["volume"] or 0) >= min_volume:
                out.append(ScreenerCandidate(
                    ticker=r["ticker"], source="fmp_movers",
                    reason=f"FMP {group} {r['change_pct']:+.1f}%",
                    pct_change=r["change_pct"], volume=r["volume"],
                    price=r["price"], priority=2))
    return out


def _screen_fq_movers(params: dict) -> list:
    """FinanceQuery day-gainers + most-actives (2026-07-16) - the KEYLESS
    open-source movers lens (github.com/Verdenroz/finance-query, hosted at
    finance-query.com, self-hostable). Zero quota, so it's the discovery
    source that always works - including with no API keys configured at
    all - and every candidate it surfaces is one FMP/AV's daily budgets
    don't have to pay for. 2h-cached inside the provider."""
    from mcp_clients.market_data import router as md_router
    if not md_router.financequery.available():
        return []
    movers = md_router.financequery.get_movers()
    if not movers:
        return []
    min_price = params.get("min_price", 5.0)
    min_volume = params.get("min_volume", 500_000)
    out = []
    for group, rows in (("gainer", movers.get("gainers", [])),
                        ("most_active", movers.get("most_active", []))):
        for r in rows:
            if r["price"] >= min_price and (r["volume"] or 0) >= min_volume:
                out.append(ScreenerCandidate(
                    ticker=r["ticker"], source="fq_movers",
                    reason=f"FQ {group} {r['change_pct']:+.1f}%",
                    pct_change=r["change_pct"], volume=r["volume"],
                    price=r["price"], priority=2))
    return out


def _screen_universe_sweep(params: dict) -> list:
    """Full-market rotation sweep (2026-07-15g) - the answer to "will it
    look at ALL stocks, not just the movers?". Draws the least-recently-
    examined batch from the persistent `universe` table (Alpaca's full
    active-US-equity asset list when keys are configured - ~10k names; plus
    organic accumulation from every other source) and feeds it through the
    SAME quality gate, discovery ranking, and scoring as every other
    candidate. Batch size bounds the data cost (each survivor costs the
    standard ranking-bars + gate-info calls); over days the whole eligible
    market rotates through scoring. Refreshes the universe from Alpaca at
    most once per day, tracked via the source_health table."""
    from storage.database import Database
    db = Database()
    batch_n = int(params.get("batch_per_cycle", 10))
    if batch_n <= 0:
        return []

    # Daily universe refresh (Alpaca assets + organic seed), throttled via
    # source_health's last_success_at for 'universe_refresh'.
    try:
        import datetime as _dt
        health = {h["name"]: h for h in db.get_source_health()}
        last = (health.get("universe_refresh") or {}).get("last_success_at") or "1970"
        stale = (\
            _dt.datetime.utcnow() - _dt.datetime.fromisoformat(last)
        ).total_seconds() > 24 * 3600 if len(last) > 6 else True
        if stale or db.universe_count() < 100:
            from mcp_clients.market_data import router as md_router
            if md_router.alpaca.assets_available():
                symbols = md_router.alpaca.get_all_assets()
                if symbols:
                    db.upsert_universe_symbols(symbols, "alpaca_assets")
                    logger.info(f"Universe refresh: {len(symbols)} active US equities from Alpaca "
                                f"(table now {db.universe_count()})")
            elif md_router.fmp.available() and db.universe_count() < 3000:
                # FMP stock-list fallback (2026-07-15h): full US directory,
                # 250-req/day free tier, one call covers it.
                symbols = md_router.fmp.get_stock_list()
                if symbols:
                    db.upsert_universe_symbols(symbols, "fmp_stock_list")
                    logger.info(f"Universe refresh: {len(symbols)} symbols from FMP stock-list "
                                f"(table now {db.universe_count()})")
            elif md_router.alphavantage.available() and db.universe_count() < 3000:
                # Alpha Vantage LISTING_STATUS fallback (2026-07-15h): full
                # US listing without Alpaca keys. Only refetched while the
                # table is small (one CSV covers it; weekly cadence via the
                # same universe_refresh throttle).
                symbols = md_router.alphavantage.get_listing_symbols()
                if symbols:
                    db.upsert_universe_symbols(symbols, "alphavantage_listing")
                    logger.info(f"Universe refresh: {len(symbols)} active US equities from "
                                f"Alpha Vantage LISTING_STATUS (table now {db.universe_count()})")
            # (2026-07-16 dedupe: a second, unreachable AV elif that called
            # the removed get_listed_symbols() was deleted here - same
            # condition as the branch above, so it could never run.)
            # organic seed always cheap
            db.upsert_universe_symbols(db.get_known_tickers_for_universe_seed(), "organic_seed")
            db.upsert_source_health("universe_refresh", True)
    except Exception as e:
        logger.warning(f"Universe refresh failed (sweep continues on existing table): {e}")

    batch = db.get_universe_sweep_batch(batch_n)
    if not batch:
        return []
    db.mark_universe_swept(batch)  # marked at draw time so failures aren't redrawn next cycle
    logger.info(f"Universe sweep batch ({len(batch)} of {db.universe_count()} known): {batch}")
    return [
        ScreenerCandidate(ticker=s, source="universe_sweep",
                           reason="full-market rotation sweep (least-recently examined)",
                           priority=3)
        for s in batch
    ]


def _screen_momentum_not_implemented(params: dict) -> list:
    _warn_once("momentum_screen", "mcp_clients/maverick.py's MaverickMCP only exposes "
               "get_all(ticker) - a per-ticker lookup, not a market-wide momentum scan.")
    return []


def _screen_insider_not_implemented(params: dict) -> list:
    _warn_once("insider_buying", "mcp_clients/stock_scanner.py's StockScannerMCP only "
               "exposes get_ticker_data(ticker) (insider trades per ticker) - no "
               "market-wide 'who's buying' scan.")
    return []
