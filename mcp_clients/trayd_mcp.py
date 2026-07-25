"""Trayd MCP client (2026-07) - HTTP client for the trayd-mcp hosted trading server.

Trayd (https://github.com/trayders/trayd-mcp) provides FULL TRADING capabilities
for Robinhood via a hosted MCP server at https://mcp.trayd.ai/mcp. Unlike the
local robinhood-mcp (read-only), trayd enables:
  - Buy/sell orders
  - Limit orders and ladder orders
  - Short selling
  - Batch operations
  - Multi-account support

DESIGN: This client uses the mcp SDK's built-in HTTP transport (HTTPClientTransport)
to communicate with the remote trayd server. It follows the same pattern as
robinhood_mcp.py (circuit breaker, caching, graceful degradation) but over HTTP
instead of stdio.

Credentials: TRAYD_MCP_URL (required) + TRAYD_AUTH_TOKEN (optional) from .env.
If not configured, all methods return empty dicts/lists and log once.

Setup: See TRAYD_MCP_SETUP.md for full integration steps.
"""
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker, run_async

logger = logging.getLogger(__name__)

# Load .env (import side effect already loaded on first import anywhere)
from mcp_clients.market_data import _load_dotenv  # noqa: E402
_load_dotenv()

# Cache TTLs
TTL_PORTFOLIO = 60        # portfolio - fresh per cycle
TTL_POSITIONS = 60        # positions - fresh per cycle
TTL_QUOTES = 60           # quotes - short-lived market data
TTL_ORDERS = 30           # open orders - very fresh
TTL_ACCOUNTS = 3600       # account list - stable

_TRAYD_CONCURRENCY = threading.Semaphore(1)  # Polite single-threaded access

_warned_no_config = False


def _config() -> Dict[str, str] | None:
    """Returns config dict if trayd is enabled, None otherwise."""
    url = os.getenv("TRAYD_MCP_URL", "").strip()
    if not url:
        return None
    config = {"url": url}
    token = os.getenv("TRAYD_AUTH_TOKEN", "").strip()
    if token:
        config["token"] = token
    return config


class TraydMCP:
    """HTTP MCP client for the trayd trading server.

    Every method degrades gracefully on failure - returns empty dict/list/None
    instead of raising exceptions. Callers must treat missing data as 'unknown',
    never as 'confirmed zero' (e.g., empty positions list could mean "unknown"
    rather than "no holdings").
    """

    def __init__(self):
        """Initialize the trayd client with config from .env."""
        self._config = _config()
        self.client = None
        self.breaker = SourceCircuitBreaker(
            "trayd", fail_threshold=3, cooldown_seconds=300
        )

        # Lazy-load the HTTP client on first use
        self._client_lock = threading.Lock()
        self._client_loaded = False

    def configured(self) -> bool:
        """Return True if trayd is configured (TRAYD_MCP_URL set in .env)."""
        return self._config is not None

    def _ensure_client(self):
        """Lazy-load the HTTP MCP client on first use."""
        if self._client_loaded:
            return

        with self._client_lock:
            if self._client_loaded:
                return

            if not self.configured():
                self._client_loaded = True
                return

            try:
                from mcp import ClientSession
                from mcp.client.http import HTTPClientTransport

                url = self._config.get("url")
                headers = {}
                if token := self._config.get("token"):
                    headers["Authorization"] = f"Bearer {token}"

                transport = HTTPClientTransport(url, extra_headers=headers)
                self.client = ClientSession(transport)
                self._client_loaded = True
                logger.info(f"trayd: HTTP client initialized - {url}")
            except ImportError:
                logger.error("trayd: mcp SDK not installed (run: pip install mcp)")
                self._client_loaded = True
            except Exception as e:
                logger.error(f"trayd: failed to initialize HTTP client - {e}")
                self._client_loaded = True

    def _call(
        self,
        tool: str,
        params: Optional[Dict[str, Any]] = None,
        cache_key: Optional[str] = None,
        ttl: int = TTL_PORTFOLIO,
    ) -> Optional[Dict[str, Any]]:
        """Call a trayd tool and return the result.

        Args:
            tool: Tool name (e.g., "trayd_get_portfolio")
            params: Tool parameters dict
            cache_key: Cache key for result (if provided, result is cached)
            ttl: Time-to-live for cache in seconds

        Returns:
            Tool result dict, or None on failure.
        """
        global _warned_no_config

        # Not configured - return None silently (warn once)
        if not self.configured():
            if not _warned_no_config:
                logger.info(
                    "trayd: TRAYD_MCP_URL not in .env - trading disabled "
                    "(platform runs normally without it)"
                )
                _warned_no_config = True
            return None

        # Check cache first
        if cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        # Check circuit breaker
        if not self.breaker.available():
            logger.debug(f"trayd: circuit breaker OPEN - skipping {tool}")
            return None

        # Ensure client is initialized
        self._ensure_client()
        if not self.client:
            logger.error("trayd: client not initialized")
            return None

        # Make the call
        try:
            with _TRAYD_CONCURRENCY:
                result = run_async(
                    self.client.call_tool(tool, params or {})
                )

            # Check if result is valid
            ok = result is not None and not (
                isinstance(result, dict) and set(result.keys()) == {"raw"}
            )

            if ok:
                self.breaker.record(True)
                if cache_key:
                    cache.set(cache_key, result, ttl)
                return result
            else:
                error = str(result.get("raw", "")) if result else "no response"
                self.breaker.record(False, error=error[:150])
                return None

        except Exception as e:
            error_msg = str(e)[:150]
            logger.error(f"trayd.{tool}: {error_msg}")
            self.breaker.record(False, error=error_msg)
            return None

    # ---- Account & Portfolio ----

    def get_portfolio(self) -> Optional[Dict[str, Any]]:
        """Get portfolio summary: total value, equity, buying power, day change.

        Returns:
            Dict with keys like 'total_value', 'equity', 'buying_power', etc.
            None if not configured or call failed.
        """
        return self._call(
            "trayd_get_portfolio",
            cache_key="trayd_portfolio",
            ttl=TTL_PORTFOLIO
        )

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all current positions with quantity, price, P&L.

        Returns:
            List of position dicts. Empty list if none or unknown.
        """
        result = self._call(
            "trayd_get_positions",
            cache_key="trayd_positions",
            ttl=TTL_POSITIONS
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            # Try common key names for wrapped results
            for key in ("positions", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_quotes(self, symbols: List[str]) -> Optional[Dict[str, Any]]:
        """Get real-time quotes for a list of symbols (24/7, live during hours).

        Args:
            symbols: List of ticker symbols (e.g., ["AAPL", "TSLA"])

        Returns:
            Dict mapping symbol -> quote data. None on failure.
        """
        if not symbols:
            return {}

        # Cache by symbol list (join and sort for consistency)
        cache_key = f"trayd_quotes_{'_'.join(sorted(symbols))}"
        return self._call(
            "trayd_get_quotes",
            {"symbols": symbols},
            cache_key=cache_key,
            ttl=TTL_QUOTES
        )

    def get_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders.

        Returns:
            List of order dicts with status, price, quantity, etc.
            Empty list if none or unknown.
        """
        result = self._call(
            "trayd_get_orders",
            cache_key="trayd_orders",
            ttl=TTL_ORDERS
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("orders", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_account_list(self) -> List[Dict[str, Any]]:
        """Get list of all Robinhood accounts for this user.

        Returns:
            List of account dicts (id, type, status, etc.).
            Empty list if none or unknown.
        """
        result = self._call(
            "trayd_get_accounts",
            cache_key="trayd_accounts",
            ttl=TTL_ACCOUNTS
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("accounts", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    # ---- Order Execution (Trading) ----

    def place_order(
        self,
        action: str,
        symbol: str,
        quantity: Optional[float] = None,
        notional: Optional[float] = None,
        limit_price: Optional[float] = None,
        order_type: str = "market",
    ) -> Optional[Dict[str, Any]]:
        """Place a buy or sell order.

        Args:
            action: "BUY" or "SELL"
            symbol: Ticker symbol (e.g., "AAPL")
            quantity: Number of shares (alternative: use notional for dollar amount)
            notional: Dollar amount to trade (alternative to quantity)
            limit_price: Price limit for limit orders. If provided, order_type
                        is automatically set to "limit"
            order_type: "market" (immediate), "limit", or "extended" (24h extended)

        Returns:
            Order confirmation dict with order_id, status, etc.
            None if call failed.

        Example:
            # Buy 10 shares of AAPL at market
            trayd.place_order("BUY", "AAPL", quantity=10)

            # Buy $500 worth of TSLA at market
            trayd.place_order("BUY", "TSLA", notional=500)

            # Sell NVDA with a $180 limit price
            trayd.place_order("SELL", "NVDA", quantity=5, limit_price=180)
        """
        params = {
            "action": action.upper(),
            "symbol": symbol.upper(),
        }

        if quantity is not None:
            params["quantity"] = quantity
        if notional is not None:
            params["notional"] = notional
        if limit_price is not None:
            params["limit_price"] = limit_price
            if order_type == "market":
                order_type = "limit"

        params["order_type"] = order_type

        # Don't cache order placement - always fresh
        return self._call("trayd_place_order", params)

    def place_ladder_order(
        self,
        symbol: str,
        action: str,
        num_orders: int,
        start_price: float,
        end_price: float,
        quantity_each: Optional[float] = None,
        notional_each: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Place a ladder order (multiple limit orders at different prices).

        Example: "Set 5 ladder buys for NVDA from $180 to $175"

        Args:
            symbol: Ticker symbol
            action: "BUY" or "SELL"
            num_orders: Number of orders to place
            start_price: Starting price (high end for buys, low end for sells)
            end_price: Ending price
            quantity_each: Quantity per order (alternative: notional_each for dollars)
            notional_each: Dollar amount per order

        Returns:
            Response dict with list of placed order IDs.
            None if call failed.
        """
        params = {
            "symbol": symbol.upper(),
            "action": action.upper(),
            "num_orders": num_orders,
            "start_price": start_price,
            "end_price": end_price,
        }

        if quantity_each is not None:
            params["quantity_each"] = quantity_each
        if notional_each is not None:
            params["notional_each"] = notional_each

        # Don't cache - order placement always fresh
        return self._call("trayd_place_ladder_order", params)

    def cancel_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Cancel a single open order by ID.

        Args:
            order_id: Order ID to cancel

        Returns:
            Confirmation dict. None if failed.
        """
        return self._call(
            "trayd_cancel_order",
            {"order_id": order_id}
        )

    def cancel_all_orders(self) -> Optional[Dict[str, Any]]:
        """Cancel all open orders.

        Returns:
            Summary dict with count of cancelled orders.
            None if failed.
        """
        return self._call("trayd_cancel_all_orders")

    # ---- Account Management ----

    def switch_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        """Switch to a different Robinhood account.

        Args:
            account_id: Target account ID (from get_account_list())

        Returns:
            Confirmation dict. None if failed.
        """
        return self._call(
            "trayd_switch_account",
            {"account_id": account_id}
        )

    def check_short_availability(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Check if a symbol can be shorted and borrowing costs.

        Args:
            symbol: Ticker symbol

        Returns:
            Dict with available, borrow_rate, shares_available, etc.
            None if failed.
        """
        return self._call(
            "trayd_check_short_availability",
            {"symbol": symbol.upper()}
        )

    # ---- Advanced (Natural Language) ----

    def execute_natural_language(self, instruction: str) -> Optional[Dict[str, Any]]:
        """Execute a trading instruction in natural language.

        Trayd parses natural language and executes complex trading instructions:
        - "Buy 10 shares of AAPL"
        - "Set 5 ladder buys for NVDA from $180 to $175"
        - "Sell my entire TSLA position"
        - "Buy $500 worth each of AAPL, GOOGL, MSFT"

        Args:
            instruction: Natural language trading instruction

        Returns:
            Response dict with execution result(s).
            None if call failed.
        """
        return self._call(
            "trayd_execute_natural_language",
            {"instruction": instruction}
        )
