"""Portfolio Risk Manager - Priority 2 architectural gap identified in the
deployment review: "You're managing individual positions very well... without
portfolio-level controls, you could unknowingly own five stocks that all
behave like one trade."

Checks a BUY candidate against the CURRENTLY OPEN positions for:
  1. Sector exposure       - max_sector_exposure_pct
  2. Theme exposure        - max_theme_exposure_pct. The hand-curated
                              ticker->theme map (AI / SEMICONDUCTORS /
                              MEGA_CAP_TECH) FIRST, since it captures real
                              cross-sector relationships no vendor labels,
                              then the cached sector/industry as an automatic
                              fallback (§18). The map alone covered 12 tickers
                              and left ~95% of traded names themeless, so the
                              cap silently never bound. Anything still
                              unclassifiable lands in UNCLASSIFIED, which has
                              its own tighter cap.
  3. Correlation           - REAL pairwise Pearson correlation of daily returns
                              (same yfinance MCP + cache pattern as
                              engine/market_breadth.py), not a sector-proxy guess
  4. Aggregate beta        - dollar-weighted average beta vs max_portfolio_beta
  5. Simultaneous high-vol - count of open positions whose risk band (stop
                              distance from entry, as a % of entry price - a
                              proxy for ATR since ATR itself isn't persisted
                              on the positions table) exceeds a config
                              threshold, capped by max_simultaneous_high_vol_positions

Primarily this returns a size_multiplier (0.0-1.0) consumed by
engine/position_sizing.py, plus reasons/warnings rendered into
output/trade_prompt.md.

§18 (Phase 2) changed the posture. hard_block_on_severe_breach now defaults
to true in config.yaml and `allowed=False` is HONOURED by scheduler.py, so a
severe breach refuses the entry rather than merely shrinking it. The previous
arrangement - a limit that is never measured and never blocks - is
documentation, not risk management. With 244 BUY signals in eight days from
momentum-based discovery, correlated clustering is the default state rather
than the exception, so this was turned on while still in paper mode
deliberately: you get to see how often it fires before it ever costs a real
trade. If it blocks constantly, that is itself the finding - it means the
screener is producing a single correlated bet dressed up as a diversified
book.

Every refusal is written to rejected_signals, so the counterfactual is
recorded rather than lost.
"""
import logging
from dataclasses import dataclass, field

from engine.cache import cache
from storage.database import Database

logger = logging.getLogger("trading")

TTL_CORRELATION = 3600  # 60 min - correlation structure moves slowly; no need to refetch every scan cycle

_DEFAULT_THEME_MAP = {}


@dataclass
class PortfolioRiskResult:
    allowed: bool
    size_multiplier: float
    sector: str
    themes: list
    sector_exposure_pct: float
    theme_exposure_pct: float
    portfolio_beta: float
    max_pairwise_correlation: float
    high_vol_position_count: int
    # §C3: how many OPEN positions were measured by the stop-distance proxy
    # rather than by persisted entry ATR, because they predate migrations/011.
    # Not a count of high-vol positions - a count of positions whose
    # high-vol-ness is an estimate. Falls to zero as the book turns over, at
    # which point high_vol_position_count is entirely measured and
    # high_vol_atr_pct_threshold can be recalibrated against it (§B6).
    high_vol_proxy_count: int = 0
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # §18: which caps were breached severely, and by how much. Kept separate
    # from `reasons` so a caller can distinguish "this was sized down" from
    # "this was refused", and so the refusal can be logged with its cause
    # rather than with the whole advisory narrative.
    severe_breaches: list = field(default_factory=list)

    @property
    def reason(self) -> str:
        """One line, for a log entry or a rejected_signals row."""
        if self.severe_breaches:
            return "; ".join(self.severe_breaches)
        if not self.allowed:
            return "portfolio risk scaled this candidate to zero size"
        return "; ".join(self.reasons or ["OK"])


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("portfolio_risk", {}) or {}


UNCLASSIFIED = "UNCLASSIFIED"


def _themes_for(ticker: str, theme_map: dict, info: dict = None) -> list:
    """Every theme bucket this ticker belongs to (§18).

    The hand-curated map FIRST: it captures real cross-sector relationships
    like "AI" that no data vendor labels, and it is the only part of this that
    encodes a human's view of what moves together.

    Then the cached sector and industry as an automatic fallback. This is the
    fix for the finding: the manual map covered 12 tickers in 4 themes, and
    none of the names actually traded (ADPT, FLYW, ERAS, XRAY, PSNL, VG, HLN,
    TAK) appeared in any of them - so theme concentration was UNMEASURED for
    every real trade, and a cap that is never measured is documentation rather
    than risk management. ticker_info_cache already holds 552 rows of this
    data; it was simply never consulted here.

    SECTOR: and INDUSTRY: prefixes keep the derived buckets distinguishable
    from hand-curated ones in logs and in portfolio_risk_log, so a breach can
    be read back as "this was a sector concentration" rather than looking like
    someone had hand-listed it.

    An unclassifiable position falls into UNCLASSIFIED, which is its own
    bucket with its own (tight) cap rather than being skipped. Unmeasured risk
    should be rationed, not ignored - skipping it is what let the theme cap
    silently never bind.
    """
    themes = {name for name, tickers in (theme_map or {}).items()
              if ticker in (tickers or [])}
    info = info or {}
    sector = info.get("sector")
    industry = info.get("industry")
    if sector and sector != "N/A":
        themes.add(f"SECTOR:{sector}")
    if industry and industry != "N/A":
        themes.add(f"INDUSTRY:{industry}")
    if not themes:
        themes.add(UNCLASSIFIED)
    return sorted(themes)


def _scale_for_cap(pre_value: float, post_value_at_full: float, cap: float) -> float:
    """Linear interpolation between pre-trade (0% of candidate added) and
    post-trade-at-full-size (100% added) - a deliberately simple
    approximation good enough for an ADVISORY size suggestion, not an exact
    algebraic solve. Returns the fraction of the candidate's full size that
    keeps the metric at or under `cap`, clamped to [0, 1]."""
    if post_value_at_full <= cap:
        return 1.0
    if pre_value >= cap:
        return 0.0
    span = post_value_at_full - pre_value
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (cap - pre_value) / span))


def _position_risk_band_pct(pos: dict) -> float:
    """FALLBACK ONLY as of §53 - see _position_atr_pct(), which is what the
    high-volatility count now calls.

    How far this position's current stop sits from entry, as a % of entry
    price. Kept because rows opened before migrations/011 have no
    entry_atr_pct and something has to be said about them; kept SEPARATE from
    _position_atr_pct() so that "we are using the proxy" remains a visible
    fact rather than an implementation detail of one function."""
    entry = pos.get("entry_price") or 0
    stop = pos.get("current_stop_price")
    if not entry or not stop:
        return 0.0
    return abs(entry - stop) / entry * 100.0


def _position_atr_pct(pos: dict) -> float:
    """This position's volatility in the SAME UNITS as the candidate's (§53).

    THE BUG THIS FIXES. The high-volatility count compared
    _position_risk_band_pct(p) - stop distance as a % of entry - against
    high_vol_atr_pct_threshold, while the candidate side passed a true ATR
    percentage (scheduler.py: atr / price * 100). One threshold, two
    quantities.

    The proxy's own defence was that wider stops track wider ATR, and that is
    true as far as it goes. Where it stops going is the clamp: scheduler.py
    seeds risk_per_share = min(max(1.2*ATR, price*1.5%), price*stop_loss_pct),
    so past a certain volatility the stop stops widening with ATR and the proxy
    saturates. A 7%-ATR name behind a 5%-clamped stop reads as 5.0 - at the
    threshold rather than clearly past it. Worse, the stop RATCHETS as a
    position moves in favour, so |entry - stop| shrinks and a position's proxy
    volatility falls over its life. The count was therefore biased low, and
    max_simultaneous_high_vol_positions was looser than it read.

    Recalibrating the threshold - which the 2026-07-25 review asked for - could
    not have fixed this. Two different quantities do not share a threshold no
    matter where the threshold is put.

    Returns the persisted entry ATR% when present, else the proxy. The fallback
    is logged at debug rather than being silent: a book that is still mostly
    pre-migration rows is being counted the old way, and that is worth being
    able to see when the recalibration in §52/§53 is done.
    """
    return _position_atr_pct_measured(pos)[0]


def _position_atr_pct_measured(pos: dict) -> tuple:
    """(volatility_pct, used_proxy). The two-value form of _position_atr_pct().

    §C3: the count that reaches the operator is a MIXTURE while any position
    predates migrations/011 - some rows measured by real entry ATR, some by the
    stop-distance proxy that reads low. That mixture was visible only in a
    debug log, i.e. nowhere the person reading the packet would look. Callers
    that report the count to a human should report the proxy share beside it,
    so "3 high-vol positions" cannot be read as three measured facts when one
    of them is an estimate biased in a known direction.
    """
    atr_pct = pos.get("entry_atr_pct")
    if atr_pct is not None:
        try:
            return float(atr_pct), False
        except (TypeError, ValueError):
            pass
    proxy = _position_risk_band_pct(pos)
    logger.debug(
        f"portfolio_risk: {pos.get('ticker')} has no entry_atr_pct (opened "
        f"before migrations/011) - counting it with the stop-distance proxy "
        f"at {proxy:.2f}%, which reads LOW for a volatile name.")
    return proxy, True


def _fetch_closes(ticker: str, lookback_days: int) -> list:
    cache_key = f"portfolio_risk_closes_{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        from mcp_clients.yfinance_mcp import YFinanceMCP
        yf = YFinanceMCP()
        raw = yf.get_price_history(ticker, period="6mo", interval="1d")
        rows = raw.get("data") or raw.get("history") or raw.get("prices") or [] if isinstance(raw, dict) else (raw or [])
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
        closes = closes[-(lookback_days + 5):] if closes else []
        cache.set(cache_key, closes, TTL_CORRELATION)
        return closes
    except Exception:
        return []


def _pearson_correlation(closes_a: list, closes_b: list) -> float | None:
    n = min(len(closes_a), len(closes_b))
    if n < 10:
        return None
    a, b = closes_a[-n:], closes_b[-n:]
    ra = [(a[i] - a[i - 1]) / a[i - 1] for i in range(1, n) if a[i - 1]]
    rb = [(b[i] - b[i - 1]) / b[i - 1] for i in range(1, n) if b[i - 1]]
    m = min(len(ra), len(rb))
    if m < 8:
        return None
    ra, rb = ra[-m:], rb[-m:]
    mean_a, mean_b = sum(ra) / m, sum(rb) / m
    cov = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(m))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((x - mean_b) ** 2 for x in rb)
    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return None
    return cov / denom


def get_pairwise_correlation(ticker_a: str, ticker_b: str, lookback_days: int = 60) -> float | None:
    """Real Pearson correlation of daily returns, cached per-ticker (not
    per-pair, so N tickers costs N fetches, not N^2). Returns None if either
    side has too little history to compute a meaningful figure - callers
    must treat None as "unknown", not "zero correlation"."""
    if ticker_a == ticker_b:
        return 1.0
    closes_a = _fetch_closes(ticker_a, lookback_days)
    closes_b = _fetch_closes(ticker_b, lookback_days)
    if not closes_a or not closes_b:
        return None
    return _pearson_correlation(closes_a, closes_b)


class PortfolioRiskEngine:
    def __init__(self, db=None):
        self.db = db or Database()

    def evaluate(self, candidate_ticker: str, candidate_sector: str, candidate_beta: float,
                 candidate_dollar_amount: float, candidate_atr_pct: float, cfg: dict,
                 candidate_industry: str = None) -> PortfolioRiskResult:
        rcfg = _cfg(cfg)
        candidate_sector = candidate_sector or "N/A"
        candidate_beta = candidate_beta if candidate_beta is not None else 1.0
        theme_map = rcfg.get("theme_map", _DEFAULT_THEME_MAP)
        # The candidate's own sector/industry are passed in by the caller, so
        # they are used directly rather than re-read from the cache - the cache
        # row for a brand-new candidate may not exist yet this cycle.
        candidate_themes = _themes_for(
            candidate_ticker, theme_map,
            {"sector": candidate_sector, "industry": candidate_industry})

        if not rcfg.get("enabled", True):
            return PortfolioRiskResult(
                allowed=True, size_multiplier=1.0, sector=candidate_sector, themes=candidate_themes,
                sector_exposure_pct=0.0, theme_exposure_pct=0.0, portfolio_beta=candidate_beta,
                max_pairwise_correlation=0.0, high_vol_position_count=0,
                reasons=["Portfolio risk manager disabled in config"],
            )

        # Book selection (WATCH-mode paper trading, 2026-07-16): in WATCH
        # mode the SIMULATED book is the portfolio being risk-managed - it
        # was cloned from the real book at seed time, so counting both would
        # double every seeded position's sector/theme/beta exposure. Outside
        # WATCH mode, only the real (confirm_fill.py) book counts, same
        # effective behavior as before paper trading existed.
        _watch = str(cfg.get("trading", {}).get("watch_execute", "")).upper() == "WATCH"
        positions = [p for p in self.db.get_all_positions(simulated=_watch)
                     if p.get("ticker") != candidate_ticker]
        info_map = self.db.get_ticker_info_bulk([p["ticker"] for p in positions])

        existing_total = sum((p.get("dollar_amount") or 0) for p in positions)
        post_total = existing_total + candidate_dollar_amount

        reasons, warnings = [], []

        # ---- 1. Sector exposure ----
        sector_cap = float(rcfg.get("max_sector_exposure_pct", 35))
        existing_sector_dollars = sum(
            (p.get("dollar_amount") or 0) for p in positions
            if (info_map.get(p["ticker"], {}) or {}).get("sector") == candidate_sector
        )
        pre_sector_pct = (existing_sector_dollars / existing_total * 100) if existing_total else 0.0
        post_sector_pct = ((existing_sector_dollars + candidate_dollar_amount) / post_total * 100) if post_total else 0.0
        mult_sector = _scale_for_cap(pre_sector_pct, post_sector_pct, sector_cap)
        if mult_sector < 1.0:
            reasons.append(
                f"Sector '{candidate_sector}' exposure {pre_sector_pct:.0f}%->{post_sector_pct:.0f}% "
                f"vs {sector_cap:.0f}% cap -> size x{mult_sector:.2f}"
            )

        # ---- 2. Theme exposure (worst of any theme the candidate belongs to) ----
        #
        # §18: membership is now COMPUTED PER POSITION rather than looked up in
        # theme_map. That is the substantive change. The map lookup could only
        # ever see hand-listed tickers, so an open position in a name nobody
        # had added contributed nothing to any theme's exposure - which is how
        # a 40% cap went unmeasured across every trade actually taken.
        theme_cap = float(rcfg.get("max_theme_exposure_pct", 40))
        unclassified_cap = float(rcfg.get("max_unclassified_exposure_pct", 25))
        mult_theme = 1.0
        worst_theme_post_pct = 0.0
        worst_theme_name, worst_theme_cap = None, theme_cap
        position_themes = {
            p["ticker"]: set(_themes_for(p["ticker"], theme_map,
                                          info_map.get(p["ticker"], {}) or {}))
            for p in positions
        }
        for theme in candidate_themes:
            existing_theme_dollars = sum(
                (p.get("dollar_amount") or 0) for p in positions
                if theme in position_themes.get(p["ticker"], set())
            )
            # UNCLASSIFIED gets its own, tighter cap. A position nobody can
            # categorise is an unmeasured risk, and unmeasured risk should be
            # rationed rather than allowed to accumulate under the same
            # allowance as a risk you can actually see.
            this_cap = unclassified_cap if theme == UNCLASSIFIED else theme_cap
            pre_theme_pct = (existing_theme_dollars / existing_total * 100) if existing_total else 0.0
            post_theme_pct = ((existing_theme_dollars + candidate_dollar_amount) / post_total * 100) if post_total else 0.0
            if post_theme_pct > worst_theme_post_pct:
                worst_theme_post_pct, worst_theme_name, worst_theme_cap = (
                    post_theme_pct, theme, this_cap)
            this_mult = _scale_for_cap(pre_theme_pct, post_theme_pct, this_cap)
            if this_mult < mult_theme:
                mult_theme = this_mult
            if this_mult < 1.0:
                reasons.append(
                    f"Theme '{theme}' exposure {pre_theme_pct:.0f}%->{post_theme_pct:.0f}% "
                    f"vs {this_cap:.0f}% cap -> size x{this_mult:.2f}"
                )

        # ---- 3. Correlation ----
        corr_threshold = float(rcfg.get("high_correlation_threshold", 0.75))
        lookback = int(rcfg.get("correlation_lookback_days", 60))
        max_cluster = int(rcfg.get("max_high_correlation_cluster", 3))
        max_corr = 0.0
        high_corr_count = 0
        for p in positions:
            corr = get_pairwise_correlation(candidate_ticker, p["ticker"], lookback)
            if corr is None:
                continue
            max_corr = max(max_corr, corr)
            if corr >= corr_threshold:
                high_corr_count += 1
        mult_corr = 1.0
        if high_corr_count >= max_cluster:
            mult_corr = 0.0
            reasons.append(
                f"{high_corr_count} open position(s) correlated >= {corr_threshold:.2f} with "
                f"{candidate_ticker} (cluster cap {max_cluster}) - these would behave like one trade"
            )
        elif high_corr_count >= 1:
            mult_corr = 0.6
            warnings.append(
                f"{high_corr_count} open position(s) correlated >= {corr_threshold:.2f} with {candidate_ticker} "
                f"(max pairwise {max_corr:.2f}) - reduced size as a diversification buffer"
            )

        # ---- 4. Aggregate beta ----
        beta_cap = float(rcfg.get("max_portfolio_beta", 1.6))
        existing_beta_weighted = sum(
            (p.get("dollar_amount") or 0) * (info_map.get(p["ticker"], {}) or {}).get("beta", 1.0) or 0.0
            for p in positions
        )
        pre_beta = (existing_beta_weighted / existing_total) if existing_total else candidate_beta
        post_beta = ((existing_beta_weighted + candidate_beta * candidate_dollar_amount) / post_total) if post_total else candidate_beta
        mult_beta = _scale_for_cap(pre_beta, post_beta, beta_cap)
        if mult_beta < 1.0:
            reasons.append(
                f"Portfolio beta {pre_beta:.2f}->{post_beta:.2f} vs {beta_cap:.2f} cap -> size x{mult_beta:.2f}"
            )

        # ---- 5. Simultaneous high-volatility positions ----
        vol_threshold = float(rcfg.get("high_vol_atr_pct_threshold", 5.0))
        max_high_vol = int(rcfg.get("max_simultaneous_high_vol_positions", 4))
        # §53: _position_atr_pct, not _position_risk_band_pct. The threshold is
        # denominated in ATR and the candidate side has always passed ATR; this
        # side was passing stop distance.
        _measured = [_position_atr_pct_measured(p) for p in positions]
        existing_high_vol = sum(1 for v, _ in _measured if v >= vol_threshold)
        # §C3: not filtered to the high-vol ones deliberately. A proxy-measured
        # position that reads BELOW the threshold is exactly the case the bias
        # would hide, so the honest denominator is every position we had to
        # estimate, not just the ones the estimate happened to flag.
        existing_high_vol_proxy = sum(1 for _, used_proxy in _measured if used_proxy)
        candidate_is_high_vol = (candidate_atr_pct or 0.0) >= vol_threshold
        mult_vol = 1.0
        if candidate_is_high_vol and existing_high_vol >= max_high_vol:
            mult_vol = 0.0
            reasons.append(
                f"{existing_high_vol} open high-volatility positions already >= cap {max_high_vol}, "
                f"and {candidate_ticker} (ATR {candidate_atr_pct:.1f}% of price) would add another"
            )
        elif candidate_is_high_vol and existing_high_vol >= max_high_vol - 1:
            mult_vol = 0.5
            warnings.append(
                f"{existing_high_vol} open high-volatility positions, near cap {max_high_vol} - reduced size"
            )

        size_multiplier = min(mult_sector, mult_theme, mult_corr, mult_beta, mult_vol)

        # ---- Severity (§18) ----
        #
        # `blocked` used to mean "some dimension scaled to zero", which is a
        # statement about the SIZING arithmetic rather than about how far the
        # book is out of line. severe_breach_multiple makes the second
        # question askable directly: at 1.5, a 35% sector cap is a warning at
        # 40% and severe at 52.5%.
        #
        # Both definitions count. A dimension that scales to zero has already
        # said the candidate should get no capital, and a metric past 1.5x its
        # cap is out of line whatever the interpolation produced.
        severe_mult = float(rcfg.get("severe_breach_multiple", 1.5))
        severe_breaches = []

        # A SHARE-OF-BOOK measure needs a book. On an empty or nearly-empty
        # portfolio the candidate is most of it by construction - the first
        # trade of the day is 100% of one sector, 100% of one theme and 100%
        # unclassified, all at once - so an unguarded severity test would
        # refuse the first few entries of every session for arithmetic
        # reasons rather than risk ones. Caught by
        # test_empty_book_never_blocks, which is the control for this whole
        # section; without it this shipped looking correct.
        #
        # Below the floor these dimensions still SIZE DOWN through
        # _scale_for_cap - they just cannot refuse. Beta and correlation are
        # deliberately NOT gated: neither is a share of the book, so both are
        # meaningful on the very first position. A 3.0-beta candidate alone is
        # a 3.0-beta portfolio, and that is a real statement about risk.
        min_positions = int(rcfg.get("min_positions_for_concentration_block", 3))
        concentration_is_meaningful = len(positions) >= min_positions

        if concentration_is_meaningful and post_sector_pct >= sector_cap * severe_mult:
            severe_breaches.append(
                f"sector '{candidate_sector}' at {post_sector_pct:.0f}% "
                f"(>= {severe_mult:g}x the {sector_cap:.0f}% cap)")
        if (concentration_is_meaningful and worst_theme_name
                and worst_theme_post_pct >= worst_theme_cap * severe_mult):
            severe_breaches.append(
                f"theme '{worst_theme_name}' at {worst_theme_post_pct:.0f}% "
                f"(>= {severe_mult:g}x the {worst_theme_cap:.0f}% cap)")
        if post_beta >= beta_cap * severe_mult:
            severe_breaches.append(
                f"portfolio beta {post_beta:.2f} (>= {severe_mult:g}x the {beta_cap:.2f} cap)")
        if high_corr_count >= max_cluster:
            severe_breaches.append(
                f"{high_corr_count} positions correlated >= {corr_threshold:.2f} "
                f"(cluster cap {max_cluster})")

        # A dimension scaling to zero also counts as blocking - but the same
        # small-book caveat applies. _scale_for_cap returns 0.0 whenever the
        # PRE-trade value already exceeds the cap, and on a two-position book
        # a single same-sector holding is already 100%, so the concentration
        # dimensions would refuse for the same arithmetic reason as above.
        # Correlation, beta and simultaneous-high-volatility are counts and
        # ratios rather than shares of the book, so a zero from any of those
        # blocks regardless of book size.
        zero_from_concentration = min(mult_sector, mult_theme) <= 0.0
        zero_from_absolute = min(mult_corr, mult_beta, mult_vol) <= 0.0
        blocked = bool(severe_breaches) or zero_from_absolute or (
            zero_from_concentration and concentration_is_meaningful)
        allowed = True
        if blocked and rcfg.get("hard_block_on_severe_breach", False):
            allowed = False
            reasons.append(
                "BLOCKED (portfolio_risk.hard_block_on_severe_breach): "
                + ("; ".join(severe_breaches) if severe_breaches
                   else "a risk dimension scaled this candidate to zero size"))

        if not reasons and not warnings:
            reasons.append("No portfolio-level exposure concerns")

        try:
            self.db.log_portfolio_risk(
                candidate_ticker, candidate_sector, candidate_themes, post_sector_pct, worst_theme_post_pct,
                post_beta, max_corr, existing_high_vol, size_multiplier, blocked, reasons + warnings,
            )
        except Exception:
            pass  # logging must never block a live sizing decision

        return PortfolioRiskResult(
            allowed=allowed, size_multiplier=round(size_multiplier, 2), sector=candidate_sector,
            themes=candidate_themes, sector_exposure_pct=round(post_sector_pct, 1),
            theme_exposure_pct=round(worst_theme_post_pct, 1), portfolio_beta=round(post_beta, 2),
            max_pairwise_correlation=round(max_corr, 2), high_vol_position_count=existing_high_vol,
            high_vol_proxy_count=existing_high_vol_proxy,
            reasons=reasons, warnings=warnings, severe_breaches=severe_breaches,
        )
