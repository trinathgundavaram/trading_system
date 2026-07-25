"""Portfolio Risk Manager - Priority 2 architectural gap identified in the
deployment review: "You're managing individual positions very well... without
portfolio-level controls, you could unknowingly own five stocks that all
behave like one trade."

Checks a BUY candidate against the CURRENTLY OPEN positions for:
  1. Sector exposure       - max_sector_exposure_pct
  2. Theme exposure        - max_theme_exposure_pct (config-defined ticker->theme map,
                              e.g. AI / SEMICONDUCTORS / MEGA_CAP_TECH - there is no
                              free, reliable "theme" data source, so this is
                              explicitly config-driven rather than a fabricated
                              auto-classification)
  3. Correlation           - REAL pairwise Pearson correlation of daily returns
                              (same yfinance MCP + cache pattern as
                              engine/market_breadth.py), not a sector-proxy guess
  4. Aggregate beta        - dollar-weighted average beta vs max_portfolio_beta
  5. Simultaneous high-vol - count of open positions whose risk band (stop
                              distance from entry, as a % of entry price - a
                              proxy for ATR since ATR itself isn't persisted
                              on the positions table) exceeds a config
                              threshold, capped by max_simultaneous_high_vol_positions

Like every other engine/ module in this codebase, this NEVER blocks a trade
by itself - it returns a size_multiplier (0.0-1.0) consumed by
engine/position_sizing.py, plus reasons/warnings rendered into
output/trade_prompt.md. config.yaml's portfolio_risk.hard_block_on_severe_breach
(default False) is the only thing that flips `allowed` to False, and even
then, "allowed" is advisory - see README.md, trades are never placed from
Python regardless.
"""
from dataclasses import dataclass, field

from engine.cache import cache
from storage.database import Database

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
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("portfolio_risk", {}) or {}


def _themes_for(ticker: str, theme_map: dict) -> list:
    return sorted(name for name, tickers in (theme_map or {}).items() if ticker in (tickers or []))


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
    """Proxy for a position's own volatility: how far its current stop sits
    from entry, as a % of entry price. Real ATR isn't persisted on the
    positions table (only computed live from a fresh ticker_data fetch), so
    this reuses data that's ALREADY on every position row instead of
    re-fetching ATR for every open position every cycle. Wider stops
    generally track wider ATR (rules/risk_rules.py-adjacent stop logic sizes
    off ATR to begin with), so this is a genuine, if indirect, proxy - not a
    fabricated placeholder."""
    entry = pos.get("entry_price") or 0
    stop = pos.get("current_stop_price")
    if not entry or not stop:
        return 0.0
    return abs(entry - stop) / entry * 100.0


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
                 candidate_dollar_amount: float, candidate_atr_pct: float, cfg: dict) -> PortfolioRiskResult:
        rcfg = _cfg(cfg)
        candidate_sector = candidate_sector or "N/A"
        candidate_beta = candidate_beta if candidate_beta is not None else 1.0
        theme_map = rcfg.get("theme_map", _DEFAULT_THEME_MAP)
        candidate_themes = _themes_for(candidate_ticker, theme_map)

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
        theme_cap = float(rcfg.get("max_theme_exposure_pct", 40))
        mult_theme = 1.0
        worst_theme_post_pct = 0.0
        for theme in candidate_themes:
            theme_tickers = set(theme_map.get(theme, []))
            existing_theme_dollars = sum(
                (p.get("dollar_amount") or 0) for p in positions if p["ticker"] in theme_tickers
            )
            pre_theme_pct = (existing_theme_dollars / existing_total * 100) if existing_total else 0.0
            post_theme_pct = ((existing_theme_dollars + candidate_dollar_amount) / post_total * 100) if post_total else 0.0
            worst_theme_post_pct = max(worst_theme_post_pct, post_theme_pct)
            this_mult = _scale_for_cap(pre_theme_pct, post_theme_pct, theme_cap)
            if this_mult < mult_theme:
                mult_theme = this_mult
            if this_mult < 1.0:
                reasons.append(
                    f"Theme '{theme}' exposure {pre_theme_pct:.0f}%->{post_theme_pct:.0f}% "
                    f"vs {theme_cap:.0f}% cap -> size x{this_mult:.2f}"
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
        existing_high_vol = sum(1 for p in positions if _position_risk_band_pct(p) >= vol_threshold)
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
        blocked = size_multiplier <= 0.0
        allowed = True
        if blocked and rcfg.get("hard_block_on_severe_breach", False):
            allowed = False

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
            reasons=reasons, warnings=warnings,
        )
