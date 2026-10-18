"""TradingAgents (TauricResearch, Apache-2.0) as a PAPER-ONLY advisory layer.

It never places or sizes an order. Each day it asks the multi-agent desk for a
rating on a short list of tickers and logs the answer (ta_decisions). Whether
the rating adds anything is decided later, by scripts/tradingagents_report.py,
comparing forward returns of BUY-rated vs SELL-rated names against SPY. Until
that shows a net-of-cost edge, ratings must not drive trading.

Why advisory: independent work (e.g. the 'Alpha Illusion' reproduction) found
agent-reported alpha shrinks sharply under costs and out-of-sample checks, and
LLM output is non-deterministic. Install is from a clone:
    git clone https://github.com/TauricResearch/TradingAgents && pip install ./TradingAgents
and ANTHROPIC_API_KEY must be set in the environment (.env). Every ticker-day is
a multi-agent LLM run, so max_tickers_per_day caps spend.
"""
import logging
import re

logger = logging.getLogger("trading")

RATINGS = ("STRONG BUY", "STRONG SELL", "OVERWEIGHT", "UNDERWEIGHT", "BUY", "SELL", "HOLD", "NEUTRAL")
_POS, _NEG = {"BUY", "STRONG BUY", "OVERWEIGHT"}, {"SELL", "STRONG SELL", "UNDERWEIGHT"}


def parse_rating(text) -> str:
    """Most specific rating word found in the decision text, else 'UNKNOWN'."""
    t = str(text).upper()
    for r in RATINGS:
        if re.search(rf"\b{r}\b", t):
            return r
    return "UNKNOWN"


def bucket(rating: str) -> str:
    return "approve" if rating in _POS else "veto" if rating in _NEG else "neutral"


def shortlist(cfg: dict, db) -> list:
    """Sector-sleeve holdings (ex-bonds) plus the configured extra tickers,
    capped by max_tickers_per_day."""
    ac = cfg.get("tradingagents_advisor", {})
    names = [p["ticker"] for p in db.get_open_sm_positions() if p["ticker"] != "IEF"]
    names += [t for t in ac.get("extra_tickers", []) if t not in names]
    return names[: int(ac.get("max_tickers_per_day", 5))]


def analyze(ticker: str, date: str, cfg: dict) -> tuple:
    """Run the TradingAgents graph once. Returns (rating, raw_text). Raises
    ImportError with install instructions if the package is missing."""
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as e:
        raise ImportError("tradingagents is not installed - see engine/tradingagents_advisor.py docstring") from e
    ac = cfg.get("tradingagents_advisor", {})
    c = DEFAULT_CONFIG.copy()
    c["llm_provider"] = "anthropic"
    c["deep_think_llm"] = ac.get("deep_model", "claude-opus-5-5")
    c["quick_think_llm"] = ac.get("quick_model", "claude-sonnet-5-5")
    c["max_debate_rounds"] = int(ac.get("max_debate_rounds", 1))
    _, decision = TradingAgentsGraph(debug=False, config=c).propagate(ticker, date)
    return parse_rating(decision), str(decision)
