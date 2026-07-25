"""fear-greed MCP - CNN Fear & Greed Index + 7 sub-indicators, via the MCP Python
SDK (stdio transport). No Claude, no API key."""
from mcp_clients.base import StdioMCPClient, run_async


class FearGreedMCP:
    def __init__(self):
        self.client = StdioMCPClient("npx", ["-y", "mcp-server-fear-greed@latest"])

    def get_index(self) -> dict:
        """Returns full Fear & Greed index with all 7 sub-indicators."""
        result = run_async(self.client.call_tool("get_fear_greed_index", {"format": "json"}))
        if not result:
            return self._defaults()
        fg = result.get("fear_and_greed", result)  # some builds return the block unwrapped
        return {
            "score": fg.get("score", 50),
            "rating": fg.get("rating", "neutral"),
            "previous_close": fg.get("previous_close", 50),
            "previous_week": fg.get("previous_1_week", 50),
            "vix_score": result.get("market_volatility_vix", {}).get("score", 50),
            "put_call_score": result.get("put_call_options", {}).get("score", 50),
            "breadth_score": result.get("stock_price_breadth", {}).get("score", 50),
            "momentum_score": result.get("market_momentum_sp500", {}).get("score", 50),
            "junk_bond_score": result.get("junk_bond_demand", {}).get("score", 50),
            "safe_haven_score": result.get("safe_haven_demand", {}).get("score", 50),
            "source": "fear-greed-mcp",
        }

    def _defaults(self):
        return {"score": 50, "rating": "neutral", "source": "default",
                "vix_score": 50, "put_call_score": 50, "breadth_score": 50,
                "momentum_score": 50, "junk_bond_score": 50, "safe_haven_score": 50}
