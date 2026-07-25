"""fred MCP - CPI, GDP, Fed funds rate, yield curve, 800K+ macro series. Via the
MCP Python SDK (stdio) against your local fred-mcp-server build. Path
overridable with the FRED_MCP_PATH env var (.env) if this runs on another machine.

2026-07-21 (missing-tool fix): every cycle was calling get_series_observations
for 4 series (fed funds, CPI, yield curve, unemployment) and failing all 4 with
'MCP error -32602: Tool get_series_observations not found', twice each (the
base client's retry-once-on-failure) - 8 doomed calls per cycle, forever. This
is the exact same failure mode already fixed in stock_scanner.py on
2026-07-16: the local fred-mcp-server build's actual tool set has drifted from
what this module assumes. Same fix - discover the real tool set once via
list_tools() (cached 24h on success, 15min on failed discovery), only call
tools that exist, skip and return defaults otherwise. One info log line
instead of 8 warnings every cycle."""
import asyncio
import logging
import os

from engine.cache import cache
from mcp_clients.base import StdioMCPClient, run_async

logger = logging.getLogger(__name__)

FRED_MCP_PATH = os.getenv("FRED_MCP_PATH", "/Users/trinathrao/fred-mcp-server/build/index.js")

TTL_TOOLS_OK = 24 * 3600      # discovered tool set (server build changes rarely)
TTL_TOOLS_FAILED = 15 * 60    # failed discovery - retry soon, don't blind for a day
_WANTED_TOOL = "get_series_observations"


class FredMCP:
    def __init__(self):
        self.client = StdioMCPClient("node", [FRED_MCP_PATH])

    async def _tool_available(self) -> bool:
        """Mirrors stock_scanner.py's _available_tools() - discover once,
        cache, and stop hammering a tool name that doesn't exist on this
        server build."""
        cached = cache.get("fred_tool_names")
        if cached is not None:
            return _WANTED_TOOL in cached
        tools = await self.client.list_tools()
        names = {t.name for t in tools}
        if names:
            cache.set("fred_tool_names", sorted(names), TTL_TOOLS_OK)
            if _WANTED_TOOL not in names:
                logger.info(
                    f"fred: server does not expose '{_WANTED_TOOL}' "
                    f"(available: {sorted(names)}) - macro fields will stay "
                    f"None/default; no calls will be attempted for them")
        else:
            # Discovery itself failed (server not installed / node hung) -
            # cache the empty set briefly so we skip fred calls this window
            # rather than firing them all just to fail one by one.
            cache.set("fred_tool_names", [], TTL_TOOLS_FAILED)
            logger.warning("fred: tool discovery failed - skipping fred calls "
                           f"for {TTL_TOOLS_FAILED // 60}min")
        return _WANTED_TOOL in names

    async def _get_macro(self) -> dict:
        if not await self._tool_available():
            return {}
        results = await asyncio.gather(
            self.client.call_tool("get_series_observations", {"series_id": "FEDFUNDS", "limit": 1}),
            self.client.call_tool("get_series_observations", {"series_id": "CPIAUCSL", "limit": 12}),
            self.client.call_tool("get_series_observations", {"series_id": "T10Y2Y", "limit": 1}),
            self.client.call_tool("get_series_observations", {"series_id": "UNRATE", "limit": 1}),
            return_exceptions=True,
        )
        fed_rate = self._extract_value(results[0])
        cpi_data = results[1] if not isinstance(results[1], Exception) else None
        yield_spread = self._extract_value(results[2])
        unemployment = self._extract_value(results[3])

        cpi_trend = self._calc_cpi_trend(cpi_data)

        return {
            "fed_funds_rate": fed_rate,
            "cpi_trend": cpi_trend,
            "yield_spread_2s10s": yield_spread,
            "yield_curve_inverted": (yield_spread or 0) < 0,
            "unemployment": unemployment,
            "source": "fred-mcp",
        }

    def _extract_value(self, result) -> float | None:
        if isinstance(result, Exception) or not result:
            return None
        obs = result.get("observations", [])
        if obs:
            try:
                return float(obs[-1].get("value", 0))
            except (TypeError, ValueError):
                return None
        return None

    def _calc_cpi_trend(self, data) -> str:
        if not data:
            return "unknown"
        obs = data.get("observations", [])
        if len(obs) >= 3:
            vals = [float(o["value"]) for o in obs[-3:] if o.get("value") not in (".", None)]
            if len(vals) >= 2:
                if vals[-1] > vals[-2]:
                    return "rising"
                if vals[-1] < vals[-2]:
                    return "falling"
        return "stable"

    def get_macro(self) -> dict:
        return run_async(self._get_macro()) or {
            "fed_funds_rate": None, "cpi_trend": "unknown",
            "yield_spread_2s10s": None, "yield_curve_inverted": False,
            "unemployment": None, "source": "default",
        }
